"""Regression proofs for complete role evidence and candidate command identity."""

from __future__ import annotations

import copy
import importlib
import json

import pytest
from opencollab.workflows import CandidateRun
from test_validation_council_workflow import FAIL, PASS, ScriptedCtx, _base_replies

from opencollab_eval.workflows import _validation_council_solve_defs as council
from opencollab_eval.workflows.validation_council_solve import validation_council_solve


def _module(name):
    return importlib.import_module(f"opencollab_eval.workflows.{name}")


def _candidate(label, *, diff=None, records=(), output=None):
    return CandidateRun(
        label=label,
        output=output or {},
        diff=diff
        or f"diff --git a/{label}.py b/{label}.py\n--- a/{label}.py\n+++ b/{label}.py\n@@ -1 +1 @@\n-old\n+new\n",
        test_records=tuple(records),
        verified_targets=(),
    )


@pytest.mark.parametrize(
    "name",
    [
        "_localization_brief",
        "_contracts_brief",
        "_cartography_brief",
        "_judge_brief",
        "_triage_brief",
        "_risks_brief",
        "_verdict_brief",
    ],
)
def test_role_evidence_preserves_long_fields_and_every_list_item(name):
    evidence = {
        "findings": "inspection notes " * 500 + "Use fieldRef and await validate(options)." + " trailing notes" * 500,
        "requirements": [{"id": f"requirement-{i}", "evidence": "\u51fd\u6570\u8c03\u7528" * 500} for i in range(30)],
        "runner_command": "python -c \"print('quoted command')\"",
        "late_requirement": "The final symbol must stay visible.",
    }
    assert json.loads(getattr(council, name)(evidence)) == evidence


def test_candidate_proposal_cap_does_not_hide_options_from_the_judge():
    candidates = {
        "tests": [
            {"id": f"T{i}", "contract_ids": [f"C{i}"], "runner_command": f"pytest tests/test_case_{i}.py"}
            for i in range(30)
        ]
    }
    assert json.loads(council._candidates_brief(candidates, cap=2)) == candidates


async def test_g11_retry_receives_middle_diagnosis_and_verifier_receives_executable_plan():
    defect = "minScore replaces minStrength; fieldRef points to Field; await validate(options)."
    first_failure = {**FAIL, "findings": "git diff is clean " * 140 + defect + " inspected source " * 100}
    replies = copy.deepcopy(_base_replies(first_failure) + _base_replies(PASS)[6:])
    command = "yarn tsc --noEmit --pretty false"
    assertion = "Weak, empty, and mismatched passphrases must prevent export."
    for value in replies:
        if isinstance(value, dict) and "tests" in value:
            value["tests"][0].update({"runner_command": command, "assertion": assertion})
    ctx = ScriptedCtx(replies)
    result = await validation_council_solve(
        ctx, {"goal": "Fix passphrase validation", "fail_to_pass": ["HIDDEN_TARGET"]}
    )
    by_label = {call["label"]: call for call in ctx.agent_calls}
    assert result["rounds"] == 2
    assert first_failure["findings"] in by_label["coder:r2"]["prompt"]
    assert defect in by_label["coder:r2"]["prompt"]
    for label in ["baseline-triage", "post-validation-triage:r1", "post-validation-triage:r2"]:
        assert command in by_label[label]["prompt"]
        assert assertion in by_label[label]["prompt"]
        assert '"contract_ids":["C1"]' in by_label[label]["prompt"]
    assert "HIDDEN_TARGET" not in "".join(call["prompt"] for call in ctx.agent_calls)


@pytest.mark.parametrize("wrapper", ["_WiringContext", "_ResilientWiringContext", "_CoderHeavyWiringContext"])
async def test_g20_variants_deliver_full_prior_reports_to_coder(wrapper):
    wiring = _module("validation_council_wiring_ablation")
    report = {"findings": "head " * 30_000 + "REPAIR_THE_MIDDLE_DEFECT" + " tail" * 30_000}
    base = ScriptedCtx(["repaired"])
    ctx = getattr(wiring, wrapper)(base)
    ctx._records["final-verifier:r1"] = report
    await ctx.agent("Coder instruction", label="coder:r2")
    prompt = base.agent_calls[0]["prompt"]
    assert report["findings"] in prompt
    encoded = prompt.split("role or rules above.\n", 1)[1]
    assert json.loads(encoded)["final-verifier:r1"] == report


@pytest.mark.parametrize(
    "module", ["evidence_action_council", "evidence_critic_repair", "validation_council_wiring_ablation"]
)
def test_other_councils_preserve_long_json_and_all_findings(module):
    value = {"findings": [f"finding {i} " + "\u4e0a\u4e0b\u6587" * 1500 for i in range(35)]}
    assert json.loads(_module(module)._bounded_json(value)) == value


def test_tournament_keeps_late_public_evidence_and_filters_private_fields():
    module = _module("validation_council_wired_tournament")
    value = {
        "findings": [
            {"text": f"public finding {i} " + "evidence " * 1500, "hidden_test_patch": "PRIVATE_PATCH"}
            for i in range(40)
        ],
        "official_result": "PRIVATE_RESULT",
    }
    clean = json.loads(module._bounded_json(value))
    assert len(clean["findings"]) == 40
    assert clean["findings"][-1]["text"] == value["findings"][-1]["text"]
    assert "PRIVATE_" not in json.dumps(clean)


@pytest.mark.parametrize("module_name", ["validation_council_wired_dual_g20", "validation_council_wired_red_rescue"])
def test_test_record_commands_and_targets_remain_exact(module_name):
    record = {
        "target": "long-target/" * 500,
        "runner": "runner " * 100,
        "command": "echo public; " * 500 + "pytest tests/test_last.py",
        "exit_code": 1,
        "verified": False,
    }
    assert _module(module_name)._public_record(record) == record


def test_different_long_commands_cannot_become_a_false_mechanical_winner():
    dual = _module("validation_council_wired_dual_g20")
    prefix = "echo public; " * 500
    base = {"target": "test_target", "runner": "pytest"}
    a = _candidate(
        "A",
        output={
            "public_test_records": [{**base, "command": prefix + "pytest test_a.py", "exit_code": 0, "verified": True}]
        },
    )
    b = _candidate(
        "B",
        output={
            "public_test_records": [{**base, "command": prefix + "pytest test_b.py", "exit_code": 1, "verified": False}]
        },
    )
    assert dual._public_red_winner(a, b) is None


def test_late_test_failure_is_retained_for_candidate_comparison():
    dual = _module("validation_council_wired_dual_g20")
    green = [
        {"target": f"t{i}", "runner": "pytest", "command": f"pytest t{i}", "exit_code": 0, "verified": True}
        for i in range(12)
    ]
    red = copy.deepcopy(green)
    red[-1].update(exit_code=1, verified=False)
    a = _candidate("A", output={"public_test_records": red})
    b = _candidate("B", output={"public_test_records": green})
    assert len(dual._candidate_records(a)) == 12
    assert dual._public_red_winner(a, b) == "B"


@pytest.mark.parametrize("name", ["validation_council_wired_dual_contract", "validation_council_triple_coder_contract"])
def test_contract_judges_receive_full_large_diffs_and_late_paths(name):
    module = _module(name)
    diff = "\n".join(_candidate(f"file_{i}").diff for i in range(300)) + "+" + "context " * 40_000 + "FINAL_FIX\n"
    candidates = {label: _candidate(label, diff=diff.replace("FINAL_FIX", label + "_FINAL_FIX")) for label in "ABC"}
    if "triple" in name:
        text, paths, truncated = module._judge_input(candidates)
    else:
        text, paths, truncated = module._judge_input(candidates["A"], candidates["B"])
    value = json.loads(text)
    assert not truncated
    for label in paths:
        assert value[label]["diff"] == candidates[label].diff
        assert len(paths[label]) == 300
        assert "file_299.py" in paths[label]


def test_candidate_tournament_retains_all_public_metadata_and_patch_safety():
    module = _module("candidate_tournament_council")
    raw = {
        "patch": _candidate("safe").diff,
        "root_cause": "cause " * 1500,
        "evidence": [
            {"anchor": "symbol " * 100, "observation": f"observation {i} " + "body " * 600} for i in range(30)
        ],
        "test_intent": [f"pytest test_{i}.py" for i in range(20)],
        "risk_tags": [f"risk_{i}" for i in range(20)],
        "confidence": "high",
    }
    candidate = module._normalize_candidate(raw, candidate_id="A", injected_paths=[])
    assert candidate["valid"]
    assert candidate["evidence"] == raw["evidence"]
    assert candidate["test_intent"] == raw["test_intent"]
    assert candidate["root_cause"] == raw["root_cause"]
    assert json.loads(module._candidate_handoff([candidate]))["candidates"][0] == candidate
    unsafe = {
        **raw,
        "patch": (
            "diff --git a/../escape.py b/../escape.py\n"
            "--- a/../escape.py\n+++ b/../escape.py\n@@ -1 +1 @@\n-old\n+new\n"
        ),
    }
    assert not module._normalize_candidate(unsafe, candidate_id="B", injected_paths=[])["valid"]
    assert not module._normalize_candidate(raw, candidate_id="C", injected_paths=["safe.py"])["valid"]


@pytest.mark.parametrize(
    "name,function",
    [
        ("validation_council_dual_coder_contract", "_contract_adjudicate"),
        ("validation_council_wired_dual_contract", "_contract_adjudicate"),
        ("validation_council_g20_coder_contract", "_contract_adjudicate"),
    ],
)
async def test_large_complete_evidence_reaches_each_contract_judge(name, function):
    module = _module(name)
    candidates = {
        label: _candidate(label, diff=_candidate(label).diff + "+" + "detail " * 45_000 + label + "_FIX\n")
        for label in "AB"
    }
    decision = {
        "winner": "B",
        "requirements_complete": True,
        "requirements": [
            {
                "requirement": "Repair the public behavior",
                "a_coverage": "not_covered",
                "b_coverage": "covered",
                "a_evidence": ["A.py"],
                "b_evidence": ["B.py"],
            }
        ],
    }
    ctx = ScriptedCtx([decision])
    winner, _result, _reason = await getattr(module, function)(
        ctx,
        goal="Repair the behavior",
        candidate_a=candidates["A"],
        candidate_b=candidates["B"],
    )
    assert winner == "B"
    assert len(ctx.agent_calls) == 1
    for candidate in candidates.values():
        assert json.dumps(candidate.diff, ensure_ascii=False)[1:-1] in ctx.agent_calls[0]["prompt"]


@pytest.mark.parametrize(
    "name,function",
    [
        ("base_team", "base_team"),
        ("base_team_single_pass", "base_team_single_pass_v1"),
    ],
)
async def test_base_team_retains_full_analyst_and_coder_reports(name, function):
    class BaseCtx(ScriptedCtx):
        async def source_changed(self):
            return True

    brief = {"root_cause": "root cause " * 2500 + "FINAL_ROOT_CAUSE", "files": ["widget.py"]}
    report = "verification observation " * 2000 + "FINAL_CODER_DIAGNOSIS"
    ctx = BaseCtx([brief, report, {"verdict": "PASS", "findings": "verified"}])
    result = await getattr(_module(name), function)(ctx, {"goal": "Repair widget"})
    assert result["status"] == "done"
    assert brief["root_cause"] in ctx.agent_calls[1]["prompt"]
    assert report in ctx.agent_calls[2]["prompt"]


def test_execution_evidence_preserves_unmatched_and_duplicate_proposals():
    from opencollab_eval.workflows._public_api import validation_execution_evidence

    proposals = {
        "tests": [
            {"id": "T1", "runner_command": "pytest test_one.py"},
            {"id": "T1", "runner_command": "pytest test_two.py"},
            {"id": "T2", "runner_command": "pytest test_other.py"},
        ]
    }
    decision = {"accepted": [{"id": "T1"}, {"id": "missing"}]}
    result = validation_execution_evidence(decision, proposals)
    assert result["candidate_proposals"] == proposals["tests"]
    assert result["accepted_candidates"] == []
    assert result["ambiguous_accepted_ids"] == ["T1"]
    assert result["unresolved_accepted_ids"] == ["missing"]
    result["candidate_proposals"][0]["runner_command"] = "changed by a consumer"
    assert proposals["tests"][0]["runner_command"] == "pytest test_one.py"


async def test_committee_v2_passes_approved_test_specification_to_executor():
    module = _module("_swe_committee_v2_impl")
    candidate = {
        "id": "T1",
        "contract_ids": ["C1"],
        "runner_command": "pytest test_regression.py",
        "assertion": "Return value matches the complete public contract.",
    }
    ctx = ScriptedCtx([{"verdict": "PASS", "accepted": [{"id": "T1"}], "rejected": []}])
    result = await module._judge_candidates(
        ctx,
        goal="Fix regression",
        tribunal={},
        candidates={"tests": [candidate]},
        stage="pre",
        cap=2,
    )
    assert result["accepted_candidates"] == [candidate]


def test_committee_retry_keeps_retry_feedback_and_nested_failure_details():
    module = _module("_swe_committee_v2_impl")
    report = {
        "retry_feedback": "Fix Field.validate(options) and await its result.",
        "failures": [{"command": "pytest test_late.py", "expected": "ready", "actual": "pending"}],
    }
    assert json.loads(module._feedback(report)) == report
