"""What a run is asked to do must not depend on which arm is asked.

The comparison these arms are built for is about how the work is organized. Any
other difference between them -- what the task text discloses, what it says
about the machine, how hard the system prompt pushes -- is read off the results
table as if it were that organization. These tests pin the differences down to
the one block that is allowed to differ.
"""

from __future__ import annotations

import json

from opencollab_eval.generation import gen_prediction_agent as gpa
from opencollab_eval.generation import gen_prediction_workflow_inputs as gpw
from opencollab_eval.generation.gen_prediction_constants import (
    AGENT_PROMPT,
    WORKFLOW_AGENT_PROMPT,
)
from opencollab_eval.generation.gen_prediction_task_text import (
    BLIND_VALIDATION_BLOCK,
    WORKSPACE_FACTS,
)

FIXTURE = {
    "repo": "acme/widget",
    "instance_id": "acme__widget-1",
    "problem_statement": "Widget.render() drops the last row.",
    "hints_text": "The off-by-one is in _rows(), see the discussion below.",
    "base_commit": "0" * 40,
    "test_patch": "diff --git a/tests/test_widget.py b/tests/test_widget.py",
    "FAIL_TO_PASS": json.dumps(["tests/test_widget.py::test_last_row"]),
    "PASS_TO_PASS": json.dumps([]),
}


def test_the_two_arms_get_byte_identical_task_text_when_both_are_blind():
    """The condition trusted host extraction requires, so it is the real case.

    ``gen_prediction_workflow`` refuses to run at all unless blind validation is
    on, so this equality — not the general case below it — is what the arms
    actually ran under.
    """
    assert gpa.build_task(FIXTURE) == gpw.build_task(FIXTURE, include_fail_to_pass=False)


def test_the_only_difference_a_disclosure_makes_is_the_trailing_block():
    """Turning the disclosure on appends; it must not edit what came before."""
    blind = gpw.build_task(FIXTURE, include_fail_to_pass=False)
    disclosed = gpw.build_task(FIXTURE, include_fail_to_pass=True)

    shared = blind[: -len(BLIND_VALIDATION_BLOCK)]
    assert disclosed.startswith(shared)
    assert "tests/test_widget.py::test_last_row" in disclosed[len(shared) :]
    assert "tests/test_widget.py::test_last_row" not in shared


def test_the_issue_hints_reach_both_arms():
    """They used to reach the workflow arm only.

    ``hints_text`` is the issue discussion, and on a SWE-bench instance it
    routinely names the function to change. One arm having it and the other not
    is a difference about where the bug is, not about how work is organized.
    """
    hint = FIXTURE["hints_text"]
    assert hint in gpa.build_task(FIXTURE)
    assert hint in gpw.build_task(FIXTURE, include_fail_to_pass=False)


def test_both_arms_are_told_where_the_answer_is_read_from():
    """Otherwise a run that left its work elsewhere cannot be read as a choice.

    An agent that leaves its edits in a teammate's worktree submits nothing, and
    the empty patch is indistinguishable from a model that chose not to finish
    unless the model was told which tree is collected.
    """
    for text in (
        gpa.build_task(FIXTURE),
        gpw.build_task(FIXTURE, include_fail_to_pass=False),
        gpw.build_task(FIXTURE, include_fail_to_pass=True),
    ):
        assert WORKSPACE_FACTS in text
        assert "/testbed" in text


def test_neither_system_prompt_carries_procedure_for_one_arm_only():
    """The prompts say who the agent is; the task text says what the task is."""
    for prompt in (AGENT_PROMPT, WORKFLOW_AGENT_PROMPT):
        lowered = prompt.lower()
        assert "git commit" not in lowered
        assert "file_write" not in lowered
        assert "apply_patch" not in lowered
        # No pacing advice: how much to explore before acting is measured here,
        # not configured for one arm.
        assert "explore" not in lowered
        assert len(prompt.strip().splitlines()) <= 3


def test_the_single_arm_asks_for_exactly_the_declared_working_bundle():
    """The list passed to ``builtin_tools`` is the one both arms declare.

    Its counterpart -- that the team's Analyst holds these six and only the
    collaboration channel on top -- is pinned in OpenCollab's
    ``tests/test_handoff_experiment_team.py``, because the team configuration
    lives in that repository. Both sides assert the same six names, so a change
    on either fails a test rather than drifting.
    """
    from opencollab_eval.generation.gen_prediction_constants import (
        WORKING_TOOL_NAMES,
    )

    assert set(WORKING_TOOL_NAMES) == {
        "apply_patch",
        "bash",
        "file_read",
        "file_write",
        "grep",
        "run_tests",
    }
    # Sorted and unique: ``builtin_tools`` rejects a duplicate, and a stable
    # order keeps the tool schemas byte-identical between the arms.
    assert list(WORKING_TOOL_NAMES) == sorted(set(WORKING_TOOL_NAMES))
