"""The Best-of-N arm: what it allocates, what it selects, and what it records.

The arm's whole content is an allocation and a selector. Everything else is the
single-agent arm, so what has to be pinned here is the small set of decisions
that would turn it into a different arm without anything failing: giving each
candidate a seat's budget instead of a share of one, letting N containers share
one ownership marker, reporting only the winner's spend, or selecting on
something the arm is not allowed to see.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest

from opencollab_eval.generation import best_of_n_selector as selector
from opencollab_eval.generation import gen_prediction_batch as batch
from opencollab_eval.generation import gen_prediction_patch, gen_prediction_snapshot

bon = pytest.importorskip("opencollab_eval.generation.gen_prediction_best_of_n")


def _candidate(index: int, patch: str, tree: str | None, **metrics) -> selector.Candidate:
    return selector.Candidate(index=index, patch=patch, tree=tree, metrics=metrics)


# --------------------------------------------------------------------------
# the selector


def test_a_candidate_with_no_patch_or_no_tree_is_not_selectable() -> None:
    # Two ways of having nothing to submit: the agent changed nothing, or it
    # changed something that does not project onto the run's baseline -- this
    # pipeline's form of "does not apply cleanly".
    chosen = selector.select_candidate(
        [
            _candidate(0, "", None),
            _candidate(1, "diff --git a/a b/a\n", None),
            _candidate(2, "diff --git a/b b/b\n", "c" * 40),
        ]
    )

    assert chosen.index == 2
    assert chosen.rule == selector.ONLY_CANDIDATE
    assert chosen.arbitrary is False


def test_candidates_that_project_to_one_tree_are_not_a_choice() -> None:
    # Same tree means same patch. Recording this as an arbitrary tie-break
    # would overstate how often the placeholder rule is actually deciding.
    chosen = selector.select_candidate(
        [
            _candidate(0, "diff --git a/a b/a\n", "a" * 40),
            _candidate(1, "diff --git a/a b/a\n+x\n", "a" * 40),
        ]
    )

    assert chosen.index == 0
    assert chosen.rule == selector.CANDIDATES_AGREE
    assert chosen.arbitrary is False


def test_differing_candidates_are_ordered_arbitrarily_and_said_to_be() -> None:
    """The count this arm is judged by.

    Ordering by tree sha is a hash comparison: deterministic, and unrelated to
    anything about the patch. Under it the arm is a single agent on 1/N of the
    budget holding N tickets, so a batch in which this clause decides most runs
    is a batch the paper cannot describe as a fixed selector picking a best
    candidate. Recording the fact is what makes that checkable afterwards.
    """
    chosen = selector.select_candidate(
        [
            _candidate(0, "diff --git a/a b/a\n", "f" * 40),
            _candidate(1, "diff --git a/b b/b\n", "b" * 40),
            _candidate(2, "diff --git a/c b/c\n", "d" * 40),
        ]
    )

    assert chosen.index == 1
    assert chosen.rule == selector.LOWEST_TREE_SHA
    assert chosen.arbitrary is True
    assert chosen.distinct_trees == 3


def test_a_run_where_nothing_applied_selects_nothing() -> None:
    chosen = selector.select_candidate([_candidate(index, "", None) for index in range(3)])

    assert chosen.index is None
    assert chosen.rule == selector.NO_CANDIDATE
    assert chosen.eligible == ()


def test_the_selector_returns_the_same_answer_for_the_same_candidates() -> None:
    # A selector that is not a function of its inputs is a second source of
    # variance inside the arm, and there would be no way to tell it apart from
    # the model's own.
    candidates = [
        _candidate(0, "diff --git a/a b/a\n", "f" * 40),
        _candidate(1, "diff --git a/b b/b\n", "b" * 40),
    ]

    assert {selector.select_candidate(candidates).index for _ in range(20)} == {1}


def test_the_selector_never_reads_the_graded_tests() -> None:
    # The generation path withholds the official test patch and FAIL_TO_PASS
    # ids and refuses to run any other way. A selector that recovered them
    # would make this the one arm that saw its own grading, and the arm would
    # be reporting an oracle rather than a procedure.
    source = Path(selector.__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]

    for sealed in ("FAIL_TO_PASS", "PASS_TO_PASS", "test_patch", "reference_patch"):
        assert sealed not in body, sealed


# --------------------------------------------------------------------------
# the allocation


def test_a_best_of_n_run_is_funded_as_one_seat_not_as_n() -> None:
    """The mistake that would make the whole arm an artifact.

    Best-of-N opens N sessions, so the obvious reading of the driver's
    "one budget per seat" rule multiplies -- and hands the arm N times the
    compute of the single-agent arm it is compared against. The paper's
    definition is the opposite: N candidates at 1/N of the budget each.
    """
    assert batch.pool_for("best-of-n", 2_000_000, None) == 2_000_000


def test_the_arm_runs_its_own_generator_and_names_no_workflow(tmp_path: Path) -> None:
    command = batch.build_command(
        arm="best-of-n",
        instance_path=tmp_path / "a-1.json",
        predictions=tmp_path / "preds-best-of-n.jsonl",
        team_config=tmp_path / "team.yaml",
        budget_per_seat=2_000_000,
        max_steps=100,
        timeout=5400.0,
        image=None,
    )

    assert command[2] == "opencollab_eval.generation.gen_prediction_best_of_n"
    assert "--workflow" not in command
    assert "--team-config" not in command
    assert command[command.index("--budget") + 1] == "2000000"


def test_the_driver_reads_the_candidate_count_off_the_arm_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bon, "CANDIDATES", 5)

    assert batch.best_of_n_candidates() == 5


@pytest.mark.asyncio
async def test_a_candidate_gets_one_nth_of_the_pool_and_of_the_wall_clock() -> None:
    seen: dict[str, object] = {}

    async def fake_run_agent(task, cid, cfg, max_steps, budget, timeout, **kwargs):
        seen.update(max_steps=max_steps, budget=budget, timeout=timeout)
        return {}

    original = bon.run_agent
    bon.run_agent = fake_run_agent
    try:
        await bon.run_candidate(
            task="t",
            cid="cid",
            cfg={},
            max_steps=100,
            budget=2_000_000,
            timeout=5400.0,
            candidates=3,
            artifact_root=Path("."),
        )
    finally:
        bon.run_agent = original

    assert seen["budget"] == 2_000_000 // 3
    assert seen["timeout"] == pytest.approx(1800.0)
    # Not divided: --max-steps is a per-session ceiling on every arm, so a team
    # of three seats gets three times the flag as well. Dividing it here would
    # make this the one arm whose sessions are held to a different ceiling.
    assert seen["max_steps"] == 100


def test_each_candidate_owns_its_own_container_marker_directory(tmp_path: Path) -> None:
    """N containers, N ownership records.

    Container ownership is recorded partly as flat files -- ``container.id``,
    ``container.name`` -- in the run directory. With one directory for all N,
    each candidate overwrites the previous candidate's markers, so a run that
    dies mid-flight leaks every container but the last with nothing naming
    them.
    """
    directories = [
        bon.candidate_run_directory(tmp_path, "acme__widget-1", index)
        for index in range(3)
    ]

    assert len(set(directories)) == 3
    for directory in directories:
        assert tmp_path in directory.parents


# --------------------------------------------------------------------------
# what one run records


#: The snapshot every fake extraction below is proved against. Built with the
#: production types rather than by hand: the proof validator checks the whole
#: shape, so a hand-written dict tests the test's idea of a proof.
_SNAPSHOT = gen_prediction_snapshot.SolverGitSnapshot(
    anonymous_head="a" * 40,
    base_tree="b" * 40,
    commit_count=1,
    remote_count=0,
    extra_git_metadata=0,
    removed_git_metadata=0,
)


def _extraction(patch: str, tree: str) -> dict:
    encoded = patch.encode("utf-8")
    return gen_prediction_patch.TrustedPatchExtraction(
        fixed_anonymous_base=_SNAPSHOT.anonymous_head,
        base_tree=_SNAPSHOT.base_tree,
        baseline_archive_sha256="c" * 64,
        baseline_archive_bytes=10,
        baseline_archive_entries=1,
        baseline_extracted_bytes=1,
        workspace_archive_sha256="d" * 64,
        workspace_archive_bytes=10,
        workspace_archive_entries=1,
        workspace_extracted_bytes=1,
        patch_sha256=hashlib.sha256(encoded).hexdigest(),
        patch_bytes=len(encoded),
        candidate_tree=tree,
        changed_paths=(),
        path_modes=(),
    ).as_dict()


def _finished_candidate(index: int, patch: str, tree: str, tokens: int, steps: int):
    return selector.Candidate(
        index=index,
        patch=patch,
        tree=tree,
        metrics={
            "workflow_status": "done",
            "execution_quiesced": True,
            "submission_eligible": True,
            "used_tokens": tokens,
            "step_count": steps,
            "generation_image_id": "sha256:" + "8" * 64,
            "solver_git_snapshot": _SNAPSHOT.as_dict(),
            "trusted_patch_extraction": _extraction(patch, tree),
            "run_summary": {
                "steps": steps,
                "tokens": tokens,
                "status": "completed",
                "reason": None,
                "duration_s": 10.0,
                "error": None,
            },
        },
    )


def test_the_run_reports_what_all_the_candidates_spent_not_what_the_winner_spent() -> (
    None
):
    """Otherwise this is the cheapest arm in the comparison, by construction.

    ``run_summary`` is the block the arms are compared on and its ``steps`` and
    ``tokens`` are documented as run totals -- on a team, the sum over the
    agents that ran. Reporting only the selected candidate's spend would
    discard two thirds of this arm's bill while it is still charged for it.
    """
    candidates = [
        _finished_candidate(0, "diff --git a/a b/a\n", "f" * 40, tokens=100, steps=3),
        _finished_candidate(1, "diff --git a/b b/b\n", "b" * 40, tokens=200, steps=5),
        _finished_candidate(2, "diff --git a/c b/c\n", "d" * 40, tokens=300, steps=7),
    ]
    chosen = selector.select_candidate(candidates)

    patch, metrics = bon.combine_metrics(
        candidates, chosen, declared_candidates=3, pool=2_000_000, timeout=5400.0
    )

    assert patch == "diff --git a/b b/b\n"
    assert metrics["run_summary"]["tokens"] == 600
    assert metrics["run_summary"]["steps"] == 15
    assert metrics["used_tokens"] == 600
    assert metrics["step_count"] == 15
    assert metrics["best_of_n"]["tokens_total"] == 600
    assert [entry["used_tokens"] for entry in metrics["best_of_n"]["candidates"]] == [
        100,
        200,
        300,
    ]


def test_the_run_records_which_rule_chose_and_whether_it_was_arbitrary() -> None:
    candidates = [
        _finished_candidate(0, "diff --git a/a b/a\n", "f" * 40, tokens=1, steps=1),
        _finished_candidate(1, "diff --git a/b b/b\n", "b" * 40, tokens=1, steps=1),
    ]

    _patch, metrics = bon.combine_metrics(
        candidates,
        selector.select_candidate(candidates),
        declared_candidates=3,
        pool=2_000_000,
        timeout=5400.0,
    )

    block = metrics["best_of_n"]
    assert block["selector"] == selector.SELECTOR_NAME
    assert block["decided_by"] == selector.LOWEST_TREE_SHA
    assert block["arbitrary_choice"] is True
    assert block["selected_index"] == 1
    assert block["candidate_budget"] == 2_000_000 // 3


def test_the_delivered_patch_keeps_the_proofs_of_the_candidate_it_came_from() -> None:
    # The integrity flags are claims about one patch. Carrying the selected
    # candidate's record forward is what keeps them true of the patch actually
    # submitted.
    candidates = [
        _finished_candidate(0, "diff --git a/b b/b\n", "b" * 40, tokens=1, steps=1),
        _finished_candidate(1, "diff --git a/a b/a\n", "f" * 40, tokens=1, steps=1),
    ]

    patch, metrics = bon.combine_metrics(
        candidates,
        selector.select_candidate(candidates),
        declared_candidates=3,
        pool=2_000_000,
        timeout=5400.0,
    )

    assert patch == "diff --git a/b b/b\n"
    assert metrics["trusted_patch_extraction"]["patch_sha256"] == hashlib.sha256(
        patch.encode("utf-8")
    ).hexdigest()
    assert metrics["patch_extraction_succeeded"] is True
    assert metrics["submission_eligible"] is True


def test_a_run_with_nothing_to_submit_is_not_reported_as_a_finished_run() -> None:
    candidates = [
        selector.Candidate(
            index=index,
            patch="",
            tree=None,
            metrics={"workflow_status": "done", "used_tokens": 5, "step_count": 1},
        )
        for index in range(3)
    ]

    patch, metrics = bon.combine_metrics(
        candidates,
        selector.select_candidate(candidates),
        declared_candidates=3,
        pool=2_000_000,
        timeout=5400.0,
    )

    assert patch == ""
    assert metrics["submission_eligible"] is False
    assert metrics["workflow_status"] == "empty_patch_after_done"
    assert metrics["best_of_n"]["selected_index"] is None


# --------------------------------------------------------------------------
# end to end, with the container layer replaced


@pytest.fixture
def _fake_containers(monkeypatch: pytest.MonkeyPatch):
    started: list[Path] = []

    def start(image, name, run_dir):
        started.append(Path(run_dir))
        return f"cid-{len(started) - 1}"

    monkeypatch.setattr(bon, "start_container_with_marker", start)
    monkeypatch.setattr(bon, "finalize_container_ownership", lambda **kwargs: None)
    monkeypatch.setattr(bon, "container_image_id", lambda cid: "sha256:" + "8" * 64)
    monkeypatch.setattr(bon, "prepare_solver_git_snapshot", lambda cid, base: _SNAPSHOT)
    monkeypatch.setattr(
        bon,
        "prepare_trusted_patch_baseline",
        lambda cid, snap: type("Baseline", (), {"cleanup": lambda self: None})(),
    )
    monkeypatch.setattr(bon, "require_container_quiescence", lambda cid: None)
    monkeypatch.setattr(
        bon, "run_with_bounded_shutdown", lambda awaitable: asyncio.run(awaitable)
    )
    return started


def _run_main(monkeypatch, tmp_path, patches: dict[str, str]) -> Path:
    instance = tmp_path / "instance.json"
    instance.write_text(
        json.dumps(
            {
                "instance_id": "task-1",
                "base_commit": "c" * 40,
                "repo": "acme/repo",
                "problem_statement": "fix it",
                "FAIL_TO_PASS": "[]",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out" / "preds-best-of-n.jsonl"
    monkeypatch.setattr(
        bon,
        "get_config",
        lambda root: {"model": "model", "provider": "provider"},
    )

    async def fake_run_agent(task, cid, cfg, max_steps, budget, timeout, **kwargs):
        return {
            "workflow_status": "done",
            "session_quiesced": True,
            "candidate_probe_eligible": True,
            "used_tokens": 100,
            "step_count": 2,
        }

    monkeypatch.setattr(bon, "run_agent", fake_run_agent)

    def extract(cid, baseline):
        patch = patches[cid]
        tree = hashlib.sha256(patch.encode("utf-8")).hexdigest()[:40]
        return patch, [], _extraction(patch, tree)

    monkeypatch.setattr(bon, "extract_patch_guarded", extract)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gen_prediction_best_of_n.py",
            "--instance-file",
            str(instance),
            "--output",
            str(output),
        ],
    )
    bon.main()
    return output


def test_a_finished_run_writes_one_row_naming_this_arm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _fake_containers
) -> None:
    patches = {
        "cid-0": "diff --git a/a b/a\n+one\n",
        "cid-1": "diff --git a/b b/b\n+two\n",
        "cid-2": "diff --git a/c b/c\n+three\n",
    }

    output = _run_main(monkeypatch, tmp_path, patches)

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["workflow"] == "best-of-n"
    assert rows[0]["model_patch"] in patches.values()
    block = rows[0]["workflow_metric"]["best_of_n"]
    assert block["candidates_declared"] == bon.CANDIDATES
    assert block["candidates_completed"] == bon.CANDIDATES
    assert block["tokens_total"] == 100 * bon.CANDIDATES


def test_every_candidate_is_started_in_its_own_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _fake_containers
) -> None:
    # The directory is where the flat container markers land, so N candidates
    # sharing one is N containers sharing one ownership record.
    _run_main(
        monkeypatch,
        tmp_path,
        {f"cid-{index}": f"diff --git a/{index} b/{index}\n" for index in range(3)},
    )

    assert len(set(_fake_containers)) == bon.CANDIDATES
    for directory in _fake_containers:
        assert (directory / "candidate.json").is_file()


def test_a_candidate_that_fails_does_not_end_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _fake_containers
) -> None:
    """N containers means N times the chance of one infrastructure fault.

    Ending the run on the first one would throw away the candidates that
    worked. It is recorded rather than swallowed: the failure lands in that
    candidate's own metrics and the run says how many candidates completed.
    """
    patches = {
        "cid-0": "diff --git a/a b/a\n+one\n",
        "cid-2": "diff --git a/c b/c\n+three\n",
    }

    output = _run_main(monkeypatch, tmp_path, patches)

    row = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    block = row["workflow_metric"]["best_of_n"]
    assert block["candidates_declared"] == 3
    assert block["candidates_completed"] == 2
    assert sorted(block["eligible_indices"]) == [0, 2]
    assert [entry["error"] for entry in block["candidates"]][1] is not None
