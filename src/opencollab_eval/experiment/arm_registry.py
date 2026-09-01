"""What each arm is given, what may differ between them, and why.

The executable form of the paper's "what is held equal across arms" table.

Some inputs differ between arms *by design* -- a team's Analyst holds
``message_agent`` and a solo agent does not, and that channel is the treatment.
Others must not differ at all. So the check this registry backs is not "the arms
are identical"; it is **the set of differences the run has equals the set of
differences declared here**. A new difference nobody wrote down turns the check
red; an existing one does not.

Three verdicts:

``EQUAL``
    The arms must agree. Any difference is a defect.
``INTENDED``
    The arms differ on purpose, and the declared per-arm values are the design.
    A value that changed, or a difference that disappeared, turns the check red.
``DEFECT``
    A difference that is known, not intended, and not yet fixed. It is declared
    so the check stays usable for finding the *next* one, and every such entry
    has to name the outcome variable it lands on -- an unfixed defect that
    reaches nothing measured is not worth carrying, and one that reaches an
    outcome is worth saying out loud in the paper.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from opencollab_eval.generation.gen_prediction_config import LLM_TRANSPORT_METRIC_KEYS

#: The arms the batch driver runs, in ``ARM_MODULES`` order. ``arm_audit``
#: checks the two lists against each other before it observes anything: this
#: list is what it iterates, so an arm the driver runs and this list omits is
#: not checked at all, and the audit would report a clean run.
ARMS: tuple[str, ...] = (
    "single",
    "best-of-n",
    "team",
    "self-collaboration",
    "self-collaboration-reading-analyst",
)

EQUAL = "equal"
INTENDED = "intended"
DEFECT = "defect"
VERDICTS = frozenset({EQUAL, INTENDED, DEFECT})


def freeze(value: Any) -> Any:
    """A hashable, order-insensitive form of a declared or observed value."""
    if isinstance(value, Mapping):
        return tuple(sorted((key, freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(freeze(item) for item in value))
    return value


@dataclass(frozen=True)
class Factor:
    """One input, its declared value on every arm, and the reason for that."""

    name: str
    verdict: str
    values: Mapping[str, Any]
    reason: str
    evidence: tuple[str, ...]
    #: For ``DEFECT`` only: the measured quantity the difference lands on.
    outcome: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(f"{self.name}: unknown verdict {self.verdict!r}")
        if not self.reason.strip():
            raise ValueError(f"{self.name}: a declared difference needs a reason")
        if not self.evidence:
            raise ValueError(f"{self.name}: a reason needs somewhere to check it")
        if self.verdict == DEFECT and not (self.outcome or "").strip():
            raise ValueError(
                f"{self.name}: a defect must name the outcome variable it lands on"
            )
        if self.verdict != DEFECT and self.outcome is not None:
            raise ValueError(
                f"{self.name}: only a defect names an outcome variable"
            )


def _every_arm(value: Any) -> dict[str, Any]:
    return {arm: value for arm in ARMS}


_SINGLE_BUNDLE = (
    "apply_patch",
    "bash",
    "file_read",
    "file_write",
    "grep",
    "run_tests",
    "submit",
)
_TEAM_WORKING_BUNDLE = (
    "apply_patch",
    "bash",
    "file_read",
    "file_write",
    "grep",
    "message_agent",
    "run_tests",
    "submit",
    "team_status",
)
_TEAM_TESTER_BUNDLE = (
    "bash",
    "file_read",
    "git_diff",
    "grep",
    "message_agent",
    "run_tests",
    "submit",
    "team_status",
)
_SCRIPTED_TESTER_BUNDLE = (
    "bash",
    "file_read",
    "git_diff",
    "grep",
    "run_tests",
    "submit",
)
_READING_ANALYST_BUNDLE = ("bash", "file_read", "grep", "run_tests", "submit")

#: The transport keys every generator writes, in the order ``freeze`` sorts
#: them into. Imported rather than restated: the point of the factor is that
#: one function supplies them to every arm.
_TRANSPORT_METRIC_KEYS = tuple(sorted(LLM_TRANSPORT_METRIC_KEYS))

#: How much of ``--timeout`` reaches the solver, as three constructions rather
#: than as a number of seconds -- the seconds are a property of one run.
WINDOW_WHOLE = "the --timeout flag, in full"
WINDOW_SHARED = "the --timeout flag divided among the candidates"
WINDOW_REMAINING = (
    "what --timeout has left after container start and repo-map preparation"
)


_FACTORS: tuple[Factor, ...] = (
    Factor(
        name="max_steps_flag",
        verdict=EQUAL,
        values=_every_arm(100),
        reason=(
            "Steps are a runaway guard, not the resource the arms are held to; "
            "tokens are. So the flag is the same number on every arm, and it is "
            "set high enough that a working run does not meet it."
        ),
        evidence=(
            "OpenCollab-Eval/src/opencollab_eval/generation/"
            "gen_prediction_constants.py:37",
            "OpenCollab-Eval/src/opencollab_eval/generation/"
            "gen_prediction_batch.py:227",
        ),
    ),
    Factor(
        name="sessions_per_run",
        verdict=INTENDED,
        values={
            "single": 1,
            "best-of-n": 3,
            "team": 3,
            "self-collaboration": 4,
            "self-collaboration-reading-analyst": 4,
        },
        reason=(
            "How many sessions a run opens is what the arms are *for*: one agent "
            "working alone, three independent tries at the same task, three seats "
            "a model may hand work to, and a script that opens "
            "analyse/implement/verify/adjudicate in order. A team seats every "
            "declared role before the first model call, so its count is its "
            "roster; a workflow's is the number of seats its own script opened on "
            "a clean round (a rejected round opens three more); Best-of-N's is "
            "its declared candidate count."
        ),
        evidence=(
            "OpenCollab/opencollab/bootstrap/scheduler_factory.py:170",
            "OpenCollab-Eval/src/opencollab_eval/workflows/self_collaboration.py:352",
        ),
    ),
    Factor(
        name="step_ceiling_per_run",
        verdict=DEFECT,
        values={
            "single": 100,
            "best-of-n": 300,
            "team": 300,
            "self-collaboration": 400,
            "self-collaboration-reading-analyst": 400,
        },
        outcome=(
            "step count, and through it the delivered patch: a run cut off at "
            "its ceiling stops writing"
        ),
        reason=(
            "``--max-steps`` is a ceiling on each *session*, and the arms open "
            "different numbers of sessions, so the same flag buys a solo agent a "
            "hundred steps and a three-seat arm three or four hundred -- four being "
            "the scripted arms' clean round; a rejected round opens three more "
            "seats and takes them to seven hundred. Best-of-N pays it three times "
            "over for the same reason and is not exempt: its candidates share one "
            "token pool and one wall clock, and the step flag alone is not "
            "divided. The "
            "constant says so in a trailing comment and nothing enforces it; the "
            "paper declares no step ceiling at all. It has bound before: at a 2M "
            "budget two single runs stopped on the step limit rather than on the "
            "budget, and one of them wrote its last successful edit on its last "
            "event. Declared here rather than fixed because fixing it changes "
            "what a run does and would make the batches already collected "
            "incomparable to later ones."
        ),
        evidence=(
            "OpenCollab-Eval/src/opencollab_eval/generation/"
            "gen_prediction_constants.py:37",
            "OpenCollab/opencollab/bootstrap/scheduler_factory.py:170",
            "OpenCollab/opencollab/bootstrap/_workflow_runtime_session.py:252",
        ),
    ),
    Factor(
        name="token_pool_per_run",
        verdict=INTENDED,
        values={
            "single": 2_000_000,
            "best-of-n": 2_000_000,
            "team": 6_000_000,
            "self-collaboration": 6_000_000,
            "self-collaboration-reading-analyst": 6_000_000,
        },
        reason=(
            "The budget is stated per seat and the pool is computed from it. A "
            "team caps each of its N seats at 1/N of the pool, so passing a team "
            "the same pool as a solo agent would give every seat a third of what "
            "that agent gets alone -- and the shortfall would then be read off "
            "the results as something about working in a team. Best-of-N takes "
            "one seat's budget and no more: its N candidates are N samplings of "
            "one seat, not N seats, and the paper gives them 1/N of a seat each."
        ),
        evidence=(
            "OpenCollab-Eval/src/opencollab_eval/generation/"
            "gen_prediction_batch.py:250",
            "OpenCollab/opencollab/domain/scheduler.py:42",
        ),
    ),
    Factor(
        name="seat_count",
        verdict=INTENDED,
        values={
            "single": 1,
            "best-of-n": 1,
            "team": 3,
            "self-collaboration": 3,
            "self-collaboration-reading-analyst": 3,
        },
        reason=(
            "A seat is a share of the pool that one agent may spend, and the "
            "number of them is the axis under study. It is read off each arm's "
            "own declaration -- the team file's roster, the workflow module's "
            "``SEATS`` -- so the pool cannot be sized for a different number than "
            "the arm caps. It is deliberately *not* 'how many agent sessions the "
            "arm opens': Best-of-N opens three and holds one seat, because its "
            "three candidates take 1/N of one seat's budget each rather than a "
            "budget each. Reading it the other way is what would fund that arm at "
            "three times the compute of the arm it is compared against."
        ),
        evidence=(
            "OpenCollab-Eval/src/opencollab_eval/generation/"
            "gen_prediction_batch.py:161",
            "OpenCollab-Eval/src/opencollab_eval/workflows/self_collaboration.py:352",
        ),
    ),
    Factor(
        name="token_budget_per_seat",
        verdict=EQUAL,
        values=_every_arm(2_000_000),
        reason=(
            "One seat is worth exactly one solo agent's budget on every arm, so "
            "a team that quietly does the job through a single agent runs on the "
            "budget that agent would have had by itself. This is the equality "
            "that rules out an allocation artifact in the cost comparison."
        ),
        evidence=(
            "OpenCollab/opencollab/domain/scheduler.py:43",
            "OpenCollab-Eval/src/opencollab_eval/workflows/self_collaboration.py:376",
        ),
    ),
    Factor(
        name="role_tool_names",
        verdict=INTENDED,
        values={
            "single": (("swe_agent", _SINGLE_BUNDLE),),
            "best-of-n": (("swe_agent", _SINGLE_BUNDLE),) * 3,
            "team": (
                ("analyst", _TEAM_WORKING_BUNDLE),
                ("coder", _TEAM_WORKING_BUNDLE),
                ("tester", _TEAM_TESTER_BUNDLE),
            ),
            "self-collaboration": (
                ("analyst", _SINGLE_BUNDLE),
                ("coder:r1", _SINGLE_BUNDLE),
                ("tester:r1", _SCRIPTED_TESTER_BUNDLE),
                ("analyst:adjudicate:r1", _SINGLE_BUNDLE),
            ),
            "self-collaboration-reading-analyst": (
                ("analyst", _READING_ANALYST_BUNDLE),
                ("coder:r1", _SINGLE_BUNDLE),
                ("tester:r1", _SCRIPTED_TESTER_BUNDLE),
                ("analyst:adjudicate:r1", _SINGLE_BUNDLE),
            ),
        },
        reason=(
            "Four declared differences and nothing else. (0) Best-of-N is the "
            "single agent's bundle, once per candidate: the arm is an allocation "
            "of compute and not a change of capability. (1) The team's roles "
            "carry ``message_agent`` and ``team_status``: that channel is the "
            "treatment. (2) A tester holds no tool that writes a file and carries "
            "``git_diff`` instead -- a declared role boundary, kept identical on "
            "the team and its scripted twin. (3) The reading-analyst arm takes "
            "the two writing tools off the analyst for the analyse phase only, "
            "which is that arm's whole definition; it gets them back to "
            "adjudicate, so the arm is not weaker over the run than the single "
            "agent it is compared against. Everything else is the single agent's "
            "seven working tools, literally: the same ``WORKING_TOOL_NAMES``."
        ),
        evidence=(
            "OpenCollab-Eval/src/opencollab_eval/generation/"
            "gen_prediction_constants.py:92",
            "OpenCollab/configs/team.handoff.experiment.yaml:78",
            "OpenCollab-Eval/src/opencollab_eval/workflows/self_collaboration.py:404",
        ),
    ),
    Factor(
        name="ask_user_reachable",
        verdict=EQUAL,
        values=_every_arm(False),
        reason=(
            "Nobody answers ``ask_user`` in an unattended batch, so a role that "
            "held it would hold a capability it cannot use -- a difference "
            "between the arms that buys nothing. It was on the team's Analyst "
            "and was removed for exactly that reason."
        ),
        evidence=(
            "OpenCollab/configs/team.handoff.experiment.yaml:67",
            "OpenCollab/opencollab/bootstrap/tool_registry.py:164",
        ),
    ),
    Factor(
        name="task_description_sha256",
        verdict=EQUAL,
        values=_every_arm(
            "55ffa6b13a984fbfe8108c5a51e26a4c9d0c12ca669f23c3acd23537ad91a5f7"
        ),
        reason=(
            "The first thing every arm is told about the task has to be the same "
            "text, or a difference in results can be read off the briefing. Two "
            "``build_task`` builders exist -- one per generator -- and they were "
            "byte-identical only under blind validation; the workflow generator "
            "refuses to run any other way. The repository listing appended to it "
            "is asked of the container by the same builder on both arms."
        ),
        evidence=(
            "OpenCollab-Eval/src/opencollab_eval/generation/"
            "gen_prediction_workflow_inputs.py:20",
            "OpenCollab-Eval/src/opencollab_eval/generation/"
            "gen_prediction_agent.py:34",
            "OpenCollab-Eval/tests/test_gen_prediction_task_text.py:46",
        ),
    ),
    Factor(
        name="system_prompt_origin",
        verdict=INTENDED,
        values={
            "single": "AGENT_PROMPT",
            "best-of-n": "AGENT_PROMPT",
            "team": "team role cards (the harness supplies no system prompt)",
            "self-collaboration": "WORKFLOW_AGENT_PROMPT",
            "self-collaboration-reading-analyst": "WORKFLOW_AGENT_PROMPT",
        },
        reason=(
            "Who the agent is told it is *is* the arm. Both harness prompts say "
            "that and stop there -- the seven imperative rules the single-agent "
            "prompt used to carry moved into the shared task text, because a "
            "comparison across arms must not be partly reading off which arm was "
            "handed a tuned prompt. A team's agents are seated with their role "
            "cards instead, which is the variable the card ladder varies."
        ),
        evidence=(
            "OpenCollab-Eval/src/opencollab_eval/generation/"
            "gen_prediction_constants.py:60",
            "OpenCollab-Eval/src/opencollab_eval/engine/evaluator_sessions.py:373",
        ),
    ),
    Factor(
        name="system_prompt_carries_repository_listing",
        verdict=DEFECT,
        values={
            "single": False,
            "best-of-n": False,
            "team": False,
            "self-collaboration": True,
            "self-collaboration-reading-analyst": True,
        },
        outcome=(
            "token cost per step, and prompt content: the system prefix is "
            "recharged on every call, and the cost ratio between arms is a "
            "reported quantity"
        ),
        reason=(
            "``run_session_or_workflow`` appends a second repository listing "
            "(up to 512 bytes of ``git ls-files``) to "
            "the system prompt for session and workflow modes and does not pass "
            "one to team mode at all. The single arm never reaches that function "
            "-- it runs through ``gen_prediction_agent.run_agent``, and neither "
            "does Best-of-N, whose candidates run through the same function -- so "
            "the two "
            "scripted arms carry a listing in their system prefix that neither of "
            "the two confirmatory arms has. It is a different listing from the "
            "one already in the task description, built by a different function "
            "with a different cut. Declared rather than fixed because removing it "
            "changes what those arms are shown."
        ),
        evidence=(
            "OpenCollab-Eval/src/opencollab_eval/engine/"
            "evaluator_task_execution.py:248",
            "OpenCollab-Eval/src/opencollab_eval/engine/evaluator.py:374",
            "OpenCollab-Eval/src/opencollab_eval/generation/"
            "gen_prediction_agent.py:222",
        ),
    ),
    Factor(
        name="container_image_expression",
        verdict=DEFECT,
        values={
            "single": "default_container_image(arch, instance_id)",
            "best-of-n": "default_container_image(arch, instance_id)",
            "team": "inline f-string, no shared helper",
            "self-collaboration": "inline f-string, no shared helper",
            "self-collaboration-reading-analyst": "inline f-string, no shared helper",
        },
        outcome=(
            "which container the run happens in -- latent on the present corpus, "
            "where every instance id is already in Docker's normal form"
        ),
        reason=(
            "The single-agent generator derives its image through "
            "``default_container_image``, which validates the instance id and "
            "maps it to a stable Docker name component; the workflow generator "
            "interpolates the id straight into an f-string. The two agree on "
            "every id that is already lowercase and Docker-safe, which is every "
            "id in the corpus, and diverge on any that is not: "
            "``PyCQA__astroid-946`` resolves to "
            "``sweb.eval.x86_64.pycqa__astroid-946-2d57c95fcd54:latest`` on one "
            "arm and to the unnormalised name on the other. In practice the "
            "batch driver passes ``--image`` explicitly whenever the instance "
            "names one, which is what has kept this latent."
        ),
        evidence=(
            "OpenCollab-Eval/src/opencollab_eval/generation/gen_prediction.py:232",
            "OpenCollab-Eval/src/opencollab_eval/generation/"
            "gen_prediction_workflow.py:705",
            "OpenCollab-Eval/src/opencollab_eval/generation/"
            "gen_prediction_config.py:66",
        ),
    ),
    Factor(
        name="wall_clock_window",
        verdict=DEFECT,
        values={
            "single": WINDOW_WHOLE,
            "best-of-n": WINDOW_SHARED,
            "team": WINDOW_REMAINING,
            "self-collaboration": WINDOW_REMAINING,
            "self-collaboration-reading-analyst": WINDOW_REMAINING,
        },
        outcome=(
            "``done_with_timeout_patch`` and the completeness of the delivered "
            "patch: a run cut off on its wall clock stops mid-edit, and whether "
            "it was cut off depends on how much of the flag it was given"
        ),
        reason=(
            "``--timeout`` is 5400 seconds on every arm, and the arms do not all "
            "get 5400 seconds of work out of it. The single-agent generator hands "
            "the flag whole to ``client.agent``. The three orchestrated arms go "
            "through ``run_session_or_workflow``, which sets the solver's timeout "
            "to what the deadline has *left* -- container acquisition, test-patch "
            "injection and the repository map are already spent by then -- so "
            "their real working window is shorter by however long that "
            "preparation took, which is a property of the machine and of the "
            "image, not of the arm. Best-of-N is short in a third way and on "
            "purpose: its candidates divide the flag, because ``--timeout`` bounds "
            "one run everywhere else and N candidates each given it whole would "
            "hand that arm N times the wall clock. Recorded as the construction "
            "and not as a number of seconds: the seconds are a property of one "
            "run, so a check that stated them would be pinned to whichever run it "
            "was written from."
        ),
        evidence=(
            "OpenCollab-Eval/src/opencollab_eval/engine/"
            "evaluator_task_execution.py:249",
            "OpenCollab-Eval/src/opencollab_eval/generation/"
            "gen_prediction_agent.py:226",
            "OpenCollab-Eval/src/opencollab_eval/generation/"
            "gen_prediction_best_of_n.py:131",
        ),
    ),
    Factor(
        name="metrics_key_set",
        verdict=EQUAL,
        values=_every_arm(_TRANSPORT_METRIC_KEYS),
        reason=(
            "How a run reached the provider -- which wire protocol, which "
            "reasoning effort, which endpoint digest, which environment switches "
            "-- is an input to every per-run reading and appears nowhere in the "
            "prediction itself. It was written by the workflow/team generator and "
            "by no other, so three arms could answer those questions and one "
            "could not, on an axis the arms are supposed to be identical on. One "
            "function now supplies the block to every generator, which is what "
            "this factor checks: not that the values match, but that each arm's "
            "generator writes the same key set."
        ),
        evidence=(
            "OpenCollab-Eval/src/opencollab_eval/generation/"
            "gen_prediction_config.py:161",
            "OpenCollab-Eval/src/opencollab_eval/generation/gen_prediction.py:302",
            "OpenCollab-Eval/src/opencollab_eval/generation/"
            "gen_prediction_workflow.py:495",
        ),
    ),
    Factor(
        name="sampling_temperature",
        verdict=EQUAL,
        values=_every_arm(1.0),
        reason=(
            "Every generator resolves its sampling settings through the same "
            "``resolve_runtime_config`` view of OpenCollab's configuration, and "
            "the batch driver pins the value in the environment it starts each "
            "generator with, so there is one place it comes from. It is 1.0 "
            "because of what the Best-of-N arm is: N independent samples of one "
            "seat. At a temperature low enough to make the model "
            "near-deterministic those N candidates are one candidate drawn N "
            "times and that arm measures nothing. Pinned in the driver rather "
            "than by changing OpenCollab's own default, which every other user "
            "of that library would get."
        ),
        evidence=(
            "OpenCollab/opencollab/bootstrap/config.py:55",
            "OpenCollab-Eval/src/opencollab_eval/generation/"
            "gen_prediction_batch.py:105",
            "OpenCollab-Eval/src/opencollab_eval/runtime_config.py:12",
        ),
    ),
)

#: Factor name to declaration.
REGISTRY: dict[str, Factor] = {factor.name: factor for factor in _FACTORS}

#: Inputs that cannot be settled without a real run, and where to read them.
#: Listed rather than guessed: a check that computes one of these from the
#: source would be asserting its own arithmetic, not the run's behaviour.
RUNTIME_ONLY: tuple[tuple[str, str], ...] = (
    (
        "steps actually taken per session",
        'metrics["run_summary"]["steps"] on every arm, and '
        'metrics["step_count"]; a run at its ceiling reports '
        'step_ceiling_reached in the session record',
    ),
    (
        "tokens actually spent per seat",
        'team: metrics["seat_spend"] / the scheduler budget table; '
        'self-collaboration: workflow_result["seat_spend"]; '
        "single: metrics[\"used_tokens\"]",
    ),
    (
        "which declared edges carried a payload",
        'self-collaboration: workflow_result["edges_walked"] against '
        '["edges_declared"]; team: the message_sent / message_refused events '
        "in the trajectory",
    ),
    (
        "whether the analyst wrote source before the coder was called",
        'workflow_result["analyst_wrote_source"] on the scripted arms; the '
        'team arm answers the same question as tree_snapshots at seat '
        "boundaries",
    ),
    (
        "the container image the run actually used",
        'metrics["generation_image_id"], which is the resolved image id rather '
        "than the name -- the only way to tell two arms apart when one of them "
        "normalised the instance id and the other did not",
    ),
    (
        "the repository listing the agents were actually shown",
        "the first user message and the system prompt in the trajectory "
        "(trajectory.jsonl / orchestration.jsonl); the listing is built from "
        "the task container, so its content is a property of the container and "
        "not of this repository",
    ),
    (
        "reasoning text",
        "not recorded at all on a non-streaming provider: the reasoning body "
        "cannot be retrieved without stream=True, so no check here can tell "
        "whether two arms reasoned differently",
    ),
)

__all__ = [
    "ARMS",
    "WINDOW_REMAINING",
    "WINDOW_SHARED",
    "WINDOW_WHOLE",
    "DEFECT",
    "EQUAL",
    "INTENDED",
    "REGISTRY",
    "RUNTIME_ONLY",
    "VERDICTS",
    "Factor",
    "freeze",
]
