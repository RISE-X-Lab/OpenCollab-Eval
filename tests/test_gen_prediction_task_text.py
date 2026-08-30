"""What a run is asked to do must not depend on which arm is asked.

The comparison these arms are built for is about how the work is organized. Any
other difference between them -- what the task text discloses, what it says
about the machine, how hard the system prompt pushes -- is read off the results
table as if it were that organization. These tests pin the differences down to
the one block that is allowed to differ.
"""

from __future__ import annotations

import json
from pathlib import Path

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

    Its counterpart -- that the team's Analyst holds these seven and only the
    collaboration channel on top -- is pinned in OpenCollab's
    ``tests/test_handoff_experiment_team.py``, because the team configuration
    lives in that repository. Both sides assert the same seven names, so a
    change on either fails a test rather than drifting.
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
        # Ending a turn on purpose is available to every arm, so that "the
        # agent said it was finished" is a fact about the model rather than
        # about which arm it was in.
        "submit",
    }
    # Sorted and unique: ``builtin_tools`` rejects a duplicate, and a stable
    # order keeps the tool schemas byte-identical between the arms.
    assert list(WORKING_TOOL_NAMES) == sorted(set(WORKING_TOOL_NAMES))


def test_the_repository_listing_is_appended_the_same_way_for_both_arms():
    """One formatter, two call sites, because the arms reach an env differently.

    An arm handed a map of the repository and an arm that has to go and list it
    are not doing the same task, and neither are two arms whose maps are laid
    out differently. The listing itself has to be taken through the environment
    -- the directory this process could walk is the one the run was launched
    from, not the one inside the task container -- so it arrives as a string and
    only the formatting is shared.
    """
    from opencollab_eval.generation.gen_prediction_task_text import (
        append_repository_layout,
    )

    repo_map = "## Repository layout\nsrc/\nsrc/widget.py\n"
    single = append_repository_layout(gpa.build_task(FIXTURE), repo_map)
    team = append_repository_layout(
        gpw.build_task(FIXTURE, include_fail_to_pass=False), repo_map
    )

    assert single == team
    assert single.endswith("src/widget.py\n")


def test_a_listing_that_could_not_be_taken_appends_nothing():
    """``build_repo_map_via_env`` returns "" when it fails, so callers append
    unconditionally. A run without a map must be a shorter prompt, not a prompt
    with an empty section in it."""
    from opencollab_eval.generation.gen_prediction_task_text import (
        REPOSITORY_LAYOUT_HEADER,
        append_repository_layout,
    )

    base = gpa.build_task(FIXTURE)
    for empty in ("", "   \n"):
        assert append_repository_layout(base, empty) == base
    assert REPOSITORY_LAYOUT_HEADER not in base


def test_a_listing_that_could_not_be_taken_says_so(capsys):
    """Silence is how this went unnoticed for a whole day in a container.

    A failed listing produces the same prompt as a run that never asked for
    one, so nothing downstream had any reason to mention it. The run log now
    does.
    """
    from opencollab_eval.generation.gen_prediction_task_text import (
        append_repository_layout,
    )

    append_repository_layout(gpa.build_task(FIXTURE), "")
    assert "repo map: unavailable" in capsys.readouterr().out

    append_repository_layout(gpa.build_task(FIXTURE), "## Repository layout\nsrc/\n")
    assert "repo map" not in capsys.readouterr().out


def test_both_arms_default_to_the_same_run_limits() -> None:
    """A ceiling that differs by arm is a ration that differs by arm.

    The two generators wrote their own numbers and had drifted apart: 40 steps
    and 900 seconds for the single agent, 60 and 1800 for the workflow and the
    team. Neither ceiling bound on the runs that had been done, so the gap left
    no trace in any result -- and a longer run, or a slower machine, is all it
    would take for one arm to be stopped by a limit the other never meets.

    Tokens are what the arms are aligned on. These two are stop-losses, so what
    is pinned is that both arms read the same value, not what the value is: the
    constants come from one module, and each CLI names them instead of writing
    a number of its own.
    """
    from opencollab_eval.generation import gen_prediction as single
    from opencollab_eval.generation import gen_prediction_workflow as workflow

    names = ("DEFAULT_BUDGET", "DEFAULT_MAX_STEPS", "DEFAULT_TIMEOUT")
    for name in names:
        assert getattr(single, name) is getattr(workflow, name), name

    generation = Path(single.__file__).parent
    for module_name, flags in (
        ("gen_prediction.py", ("--budget", "--max-steps", "--timeout")),
        ("gen_prediction_workflow.py", ("--budget", "--max-steps", "--timeout")),
    ):
        source = (generation / module_name).read_text(encoding="utf-8")
        for flag in flags:
            line = next(
                text for text in source.splitlines()
                if f'add_argument("{flag}"' in text
            )
            assert "default=DEFAULT_" in line, (module_name, flag, line)


def test_the_token_budget_is_the_only_ceiling_a_working_run_can_reach() -> None:
    """Raising the budget alone changes which limit stops a productive run.

    Both ceilings are stop-losses, so a run that is still producing has to be
    stopped by the token budget rather than by the step count. A batch at 2M
    tokens per seat put that to the test on 2026-08-29: mwaskom__seaborn-3069
    and pylint-dev__astroid-946 both ended on a step limit of 60 while still
    writing source, and seaborn's last successful write was its last event.

    A step is charged for the whole conversation before it, so late steps are
    the expensive ones; the priciest run measured over that batch averaged
    about 31k tokens per step. The step ceiling therefore has to be at least
    the budget divided by that figure, or raising the budget just hands the
    stop to the other ceiling.

    The wall clock is the third ceiling and takes the same treatment. The
    slowest run measured at 2M spent 1,951,723 tokens in 2,234 seconds, so a
    seat's worth of tokens takes on the order of 2,300 seconds to spend; the
    old 1800-second default would have cut that run off mid-run.
    """
    from opencollab_eval.generation import gen_prediction_constants as const

    observed_tokens_per_step = 31_000  # 1,865,210 tokens over 60 steps, seaborn at 2M
    assert const.DEFAULT_MAX_STEPS >= const.DEFAULT_BUDGET / observed_tokens_per_step

    observed_tokens_per_second = 874  # 1,951,723 tokens in 2,234 s, astroid at 2M
    assert const.DEFAULT_TIMEOUT >= const.DEFAULT_BUDGET / observed_tokens_per_second
