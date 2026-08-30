"""What a run is asked to do must not depend on which arm is asked.

The comparison these arms are built for is about how the work is organized. Any
other difference between them -- what the task text discloses, what it says
about the machine, how hard the system prompt pushes -- is read off the results
table as if it were that organization. These tests pin the differences down to
the one block that is allowed to differ.
"""

from __future__ import annotations

import json
import re
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

# The properties below are checked against families of terms rather than
# against the sentences themselves. A prompt is edited for wording constantly
# -- a clause moves, a synonym replaces a word -- and a test that pins the
# literal string goes red on every one of those without a property having
# changed. What must not change is that the text still tells every arm what is
# scored, what finishing looks like, what the machine it runs on can and cannot
# do, and what a step costs. A family is wide enough that a rewrite inside it
# stays green and narrow enough that deleting the paragraph turns it red.
_SOURCE_UNIT = ("source", "source code", "code")
_SCORING = ("scored", "graded", "counts", "credit")
_UNSCORED_ARTIFACTS = (
    "documentation",
    "docs",
    "release note",
    "release notes",
    "changelog",
    "comment",
    "comments",
)
_NOT_SCORED = (
    "not scored",
    "not graded",
    "does not count",
    "do not count",
    "no credit",
    "not read",
    "never read",
)
_FINISHED = ("finished", "done", "complete", "stop")
_TESTS = ("test", "tests", "suite")
_MINIMAL = ("minimal", "smallest", "root cause", "narrowest")
_REPEAT = ("again", "repeat", "repeating", "repeated", "re-run", "rerun", "reconfirm")
_SAME_ANSWER = (
    "return",
    "returns",
    "returned",
    "the same",
    "no evidence",
    "nothing new",
    "know already",
    "already know",
    "already hold",
)
_BUDGET = ("budget", "token", "tokens")
_FINITE = ("limited", "finite", "not extended", "runs out", "is gone", "gone")
_STEPS = ("step", "steps")
_NO_NETWORK = ("no network", "network access", "no internet", "offline", "network")
_SUPPLY = (
    "install",
    "installed",
    "reinstalled",
    "download",
    "fetch",
    "dependencies",
    "built",
)
# What a further step costs is a fact about the machine, so the text may state
# it. When to stop reading is not: a threshold over consecutive steps paces the
# run, and the explore/act split is part of what this comparison measures --
# see ``test_the_task_text_prices_a_repeated_read_without_pacing_the_run``.
# Pricing a step is not the whole of it: the run that read for an entire budget
# and wrote nothing was re-reading, and that a re-read returns the first answer
# is a fact about the machine rather than a rule about when to stop. Without
# this the sentence can be deleted with every other test still green.
_READ_AGAIN = ("a second time", "already read", "already searched", "read again")
_STEP_COST = ("charged", "costs", "cost", "paid", "price", "full price")
_NOT_FREE = ("not free", "whole conversation", "came before", "more than")

# A pacing directive is a count of the model's own recent steps paired with an
# instruction to change what it does next. Banning three phrasings does not
# pin that: a neutrally worded threshold walks straight through a phrase list,
# which is how one got back into the text. So the property is pinned instead --
# no paragraph may carry a step-count expression and a switch-to-acting verb at
# once. Each family alone is fine: the text says what a further step costs, and
# it says to change the source, just never one as the trigger for the other.
_STEP_TALLY = (
    "steps in a row",
    "consecutive steps",
    "several steps",
    "a few steps",
    "two or three steps",
    "the last few steps",
    "the next read",
    "the next step",
    "no fact you did not already have",
    "no new information",
)
_SWITCH_TO_ACTING = (
    "act on",
    "stop searching",
    "stop reading",
    "make the best change",
    "make a change",
    "change the source",
    "edit the source",
    "start editing",
    "move on",
)

#: Words that would make one sentence of the shared text mean different things
#: to an agent working alone and to an agent holding a seat in a team. The text
#: is byte-identical by construction; this is the other half -- that identical
#: bytes also carry identical instructions.
_ARM_SPECIFIC = (
    "teammate",
    "teammates",
    "colleague",
    "colleagues",
    "delegate",
    "delegation",
    "coordinate",
    "hand off",
    "handoff",
    "message_agent",
    "team_status",
    "team",
    "alone",
    "by yourself",
    "on your own",
    "your role",
)

#: The text has to be true of any instance in any suite. Naming the suite, or
#: the machinery that grades it, would make it true of the runs it was written
#: for and of nothing else.
_INSTANCE_SPECIFIC = (
    "swe-bench",
    "swebench",
    "benchmark",
    "fail_to_pass",
    "pass_to_pass",
    "django",
    "seaborn",
    "astropy",
    "matplotlib",
    "pydicom",
    "astroid",
)


def _shared_guidance() -> str:
    """The two blocks every arm receives verbatim, with nothing instance-shaped.

    ``_CLOSING`` is private to the module and read here on purpose: it is half
    of what both arms are told, so a property about the shared text that
    skipped it would be checking one paragraph of a two-paragraph claim.
    """
    from opencollab_eval.generation.gen_prediction_task_text import _CLOSING

    return f"{WORKSPACE_FACTS}\n{_CLOSING}\n"


def _paragraphs(text: str) -> list[str]:
    """Paragraphs, unwrapped and lowercased, so line breaks do not matter."""
    return [
        " ".join(block.split()).lower()
        for block in text.split("\n\n")
        if block.strip()
    ]


def _mentions(paragraph: str, family: tuple[str, ...]) -> bool:
    return any(
        re.search(rf"(?<!\w){re.escape(term)}(?!\w)", paragraph) for term in family
    )


def _paragraphs_saying(text: str, *families: tuple[str, ...]) -> list[str]:
    """Paragraphs that mention at least one term from every family given."""
    return [
        paragraph
        for paragraph in _paragraphs(text)
        if all(_mentions(paragraph, family) for family in families)
    ]


def _arm_texts() -> tuple[str, ...]:
    return (
        gpa.build_task(FIXTURE),
        gpw.build_task(FIXTURE, include_fail_to_pass=False),
        gpw.build_task(FIXTURE, include_fail_to_pass=True),
    )


def test_every_arm_receives_the_closing_instruction_as_well_as_the_facts():
    """Both constants are the shared half; a builder may only append after them."""
    from opencollab_eval.generation.gen_prediction_task_text import _CLOSING

    for text in _arm_texts():
        assert WORKSPACE_FACTS in text
        assert _CLOSING in text


def test_the_task_text_names_the_unit_that_is_scored():
    """Without it the model has to guess, and it guessed like a contributor.

    The text named the tests a fix is graded against and stopped there, so a run
    that had a working fix in the tree could go on to produce the rest of a
    plausible pull request. Naming the scored unit is a fact about the task,
    identical for every arm and every instance.
    """
    for text in _arm_texts():
        assert _paragraphs_saying(text, _SOURCE_UNIT, _SCORING), text


def test_the_task_text_names_the_work_that_is_not_scored():
    """Naming the scored unit is not enough; the sinks have to be named too."""
    for text in _arm_texts():
        stated = _paragraphs_saying(text, _UNSCORED_ARTIFACTS, _NOT_SCORED)
        assert stated, text
        # More than one kind of unscored artifact, so the claim reads as a
        # category and not as one example the model can route around.
        assert any(
            sum(_mentions(paragraph, (term,)) for term in _UNSCORED_ARTIFACTS) >= 2
            for paragraph in stated
        ), stated


def test_the_unscored_work_is_marked_unscored_and_not_forbidden():
    """What the run spends a fixed budget on is the behavior being measured.

    A prohibition would remove the choice, and with it the measurement: a model
    that writes no release notes because it was told not to has told us nothing
    about how it allocates a budget. The text has to state the price and leave
    the decision.
    """
    guidance = " ".join(_shared_guidance().split()).lower()
    artifacts = "|".join(re.escape(term) for term in _UNSCORED_ARTIFACTS)
    prohibitions = (
        rf"(?<!\w)(do not|don't|never|must not|avoid|refrain from)(?!\w)"
        rf"[^.]{{0,60}}(?<!\w)({artifacts})(?!\w)",
        rf"(?<!\w)({artifacts})(?!\w)[^.]{{0,60}}"
        rf"(?<!\w)(are forbidden|is forbidden|are prohibited|are not allowed)(?!\w)",
    )
    for pattern in prohibitions:
        assert re.search(pattern, guidance) is None, pattern


def test_the_task_text_says_what_finishing_looks_like():
    """A run with no stated stop condition stops when the budget does.

    Minimal, aimed at the root cause, and confirmed by the project's own tests:
    the three together are the condition, so all three have to be stated in one
    place rather than implied across the document.
    """
    for text in _arm_texts():
        assert _paragraphs_saying(text, _FINISHED, _TESTS, _MINIMAL), text


def test_the_task_text_says_a_test_run_already_held_is_not_new_evidence():
    """This is where the budget actually went.

    Across the measured runs the median one spent well over half of its budget
    after its last edit to the source, and most of that tail was the same tests
    run again, or a wider suite run to reconfirm a pass already in hand. The
    text has to say that a pass already held is the result, not a claim that
    another run will strengthen.
    """
    for text in _arm_texts():
        assert _paragraphs_saying(text, _TESTS, _REPEAT, _SAME_ANSWER), text


def test_the_task_text_says_the_budget_is_finite():
    for text in _arm_texts():
        assert _paragraphs_saying(text, _BUDGET, _FINITE), text


def test_the_task_text_says_what_one_more_step_costs():
    """A step is charged for everything before it, so steps are not alike.

    Nearly all of what a step costs is the conversation it re-reads, which is
    why the step ceiling has never bound a run while the token budget has. A
    model that thinks a cheap look is cheap is reasoning about a linear cost
    that does not exist.
    """
    for text in _arm_texts():
        assert _paragraphs_saying(text, _STEPS, _STEP_COST), text


def test_the_task_text_says_the_environment_has_no_network():
    """Most runs tried to download or install something, and every attempt
    failed: the container has no network. The failures were not free -- the
    most expensive of them cost a large slice of a budget in error output --
    and one run tried to reinstall the very package it was being graded on."""
    for text in _arm_texts():
        assert _paragraphs_saying(text, _NO_NETWORK, _SUPPLY), text


def test_the_task_text_prices_a_repeated_read_without_pacing_the_run():
    """Two runs read for an entire budget and finished with an empty tree.

    The shared shape was the same terms searched in the same files over and
    over. What the text may say about that is the price: that a step is charged
    for the whole conversation before it, so reading is not free.

    What it may not say is when to stop. A draft carried "if two or three steps
    in a row have produced no fact you did not already have, stop searching,
    make the best change to the source you can justify". Those bytes are equal
    across the arms and unequal in effect: for an agent holding a seat in a
    team, the moment that sentence describes is the moment a teammate would be
    asked, and it resolves that moment towards working alone. Whether the work
    is delegated is the outcome this comparison exists to measure, so the
    shared text does not get to push on it -- and a gate that fires on the same
    condition belongs in the harness, where it applies to both arms alike and
    leaves a record each time it fires.
    """
    for text in _arm_texts():
        assert _paragraphs_saying(text, _STEP_COST, _NOT_FREE), text
        assert _paragraphs_saying(text, _READ_AGAIN, _SAME_ANSWER), text
        assert not _paragraphs_saying(text, _STEP_TALLY, _SWITCH_TO_ACTING), text


def test_the_shared_text_says_the_same_thing_to_both_arms():
    """Byte-identical is only half of it; identical bytes can still be advice
    that applies to one arm. A sentence about teammates is inert for an agent
    working alone and is a procedure for an agent holding a seat in a team, so
    the two arms would be reading different instructions off one string."""
    guidance = " ".join(_shared_guidance().split()).lower()
    for term in _ARM_SPECIFIC:
        assert not _mentions(guidance, (term,)), term


def test_the_shared_text_is_true_of_any_instance():
    guidance = " ".join(_shared_guidance().split()).lower()
    for term in _INSTANCE_SPECIFIC:
        assert not _mentions(guidance, (term,)), term
