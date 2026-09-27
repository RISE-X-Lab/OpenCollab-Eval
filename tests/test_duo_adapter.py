"""Exercise the evaluator's Duo registration, input policy, and compatibility."""

from __future__ import annotations

import pytest
from opencollab.builtin_workflows import (
    duo,
    get_builtin_workflows,
)
from opencollab.patches import patch_paths
from opencollab.tools import BashEvidence, evidence_tools, has_pass_evidence

from opencollab_eval import patch_diff, verification, workflows
from opencollab_eval.generation.gen_prediction_workflow import _bundled_workflow_registry
from opencollab_eval.generation.gen_prediction_workflow_inputs import (
    _resolve_blind_validation,
    build_extras,
    build_task,
)
from opencollab_eval.verification import _test_results as legacy_results
from opencollab_eval.verification import bash_evidence
from opencollab_eval.workflows import validation_council_dual_coder_contract as g21


@pytest.mark.parametrize(("name", "implementation"), [
    ("duo", duo),
])
def test_registry_uses_oc_implementation_and_preserves_each_workflow_identity(name, implementation):
    assert get_builtin_workflows().get(name).fn is implementation
    assert _bundled_workflow_registry()[name] is implementation
    assert implementation.__workflow_spec__.name == name
    assert _resolve_blind_validation(implementation, None) is True
    assert _resolve_blind_validation(implementation, None, name) is True
    assert _resolve_blind_validation(implementation, False, name) is False


def test_only_one_duo_workflow_is_exposed():
    assert workflows.duo is duo
    assert [spec.name for spec in get_builtin_workflows().list_specs()] == ["duo"]
    registry = _bundled_workflow_registry()
    assert "duo-v3" not in registry
    assert "validation-council-dual-coder-selection-v2" not in registry
    assert "validation-council-dual-coder-selection-v3" not in registry


def test_blind_duo_inputs_preserve_public_task_and_withhold_grading_fields():
    instance = {
        "repo": "example/project",
        "problem_statement": "Repair the documented public return value.",
        "hints_text": "Inspect the public parser.",
        "FAIL_TO_PASS": '["PRIVATE_TARGET"]',
        "test_patch": "PRIVATE_PATCH",
    }
    task = build_task(instance, include_fail_to_pass=False)
    assert instance["problem_statement"] in task
    assert instance["hints_text"] in task
    assert "PRIVATE_" not in task
    assert build_extras(instance, include_hidden_tests=False) == {"blind_validation": True}


@pytest.mark.asyncio
async def test_g21_compatibility_entry_passes_its_prompt_to_oc_execution(monkeypatch):
    calls = []
    outcome = {"status": "done", "adopted": "A"}

    async def run(ctx, args, **options):
        calls.append((ctx, args, options))
        return outcome

    monkeypatch.setattr(g21, "run_dual_coder", run)
    context, arguments = object(), {"description": "Repair public behavior"}
    assert await g21.validation_council_dual_coder_contract_v1(context, arguments) is outcome
    assert calls == [(context, arguments, {"selector_prompt": g21.CONTRACT_PROMPT,
        "coder_prompts": (g21.MINIMAL_CODER_PROMPT, g21.CROSS_COMPONENT_CODER_PROMPT),
        "role_rules": g21.SHARED_RULES})]
    assert g21.validation_council_dual_coder_contract_v1.__workflow_spec__.name == (
        "validation-council-dual-coder-contract-v1"
    )


def test_evidence_and_patch_compatibility_imports_use_oc_public_implementations():
    assert verification.BashEvidence is bash_evidence.BashEvidence is BashEvidence
    assert verification.evaluation_tools is evidence_tools
    assert legacy_results.has_pass_evidence is has_pass_evidence
    assert patch_diff.patch_paths is patch_paths
    tools = verification.evaluation_tools("bash", "file_read")
    assert isinstance(tools[0], BashEvidence)
    assert [tool.name for tool in tools] == ["bash", "file_read"]
