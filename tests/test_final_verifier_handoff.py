from __future__ import annotations

import string

from opencollab_eval.workflows import _validation_council_solve_defs as council


def test_final_verifier_receives_every_role_report_and_complete_risk():
    fields = {
        "rules", "goal", "localization", "contracts", "pre_judge", "baseline_triage",
        "coder_report", "patch_verdict", "risks", "post_judge", "post_triage",
    }
    values = {field: f"unique_{field}_" + "x" * 4000 + f"_end_{field}" for field in fields}
    actual_fields = {field for _, field, _, _ in string.Formatter().parse(council.FINAL_VERIFIER_PROMPT) if field}
    assert actual_fields == fields
    prompt = council.FINAL_VERIFIER_PROMPT.format(**values)
    for value in values.values():
        assert value in prompt


def test_commands_are_complete_after_candidate_and_judge_handoff():
    command = "python -m pytest " + "tests/" * 100 + "test_regression.py::test_case"
    candidate = {"tests": [{"id": "p1", "runner_command": command}]}
    assert command in council._candidates_brief(candidate, cap=1)
