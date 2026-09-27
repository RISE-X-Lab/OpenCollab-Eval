"""Exercise the evaluator's Duo registration, input policy, and compatibility."""

from __future__ import annotations

from opencollab.builtin_workflows import duo
from opencollab.patches import patch_paths
from opencollab.tools import BashEvidence, evidence_tools, has_pass_evidence

from opencollab_eval import patch_diff, verification
from opencollab_eval.generation.gen_prediction_workflow_inputs import (
    _resolve_blind_validation,
    build_extras,
    build_task,
)
from opencollab_eval.verification import _test_results as legacy_results
from opencollab_eval.verification import bash_evidence
from opencollab_eval.workflow_loader import load_workflow


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




def test_evidence_and_patch_compatibility_imports_use_oc_public_implementations():
    assert verification.BashEvidence is bash_evidence.BashEvidence is BashEvidence
    assert verification.evaluation_tools is evidence_tools
    assert legacy_results.has_pass_evidence is has_pass_evidence
    assert patch_diff.patch_paths is patch_paths
    tools = verification.evaluation_tools("bash", "file_read")
    assert isinstance(tools[0], BashEvidence)
    assert [tool.name for tool in tools] == ["bash", "file_read"]


def test_explicit_public_duo_entry_uses_the_oc_workflow():
    implementation = load_workflow("opencollab.builtin_workflows:duo")
    assert implementation is duo
    assert implementation.__workflow_spec__.name == "duo"
    assert _resolve_blind_validation(implementation, None) is True
    assert _resolve_blind_validation(implementation, None, "caller-label") is True
    assert _resolve_blind_validation(implementation, False, "caller-label") is False
