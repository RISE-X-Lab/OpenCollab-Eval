"""A dropped run's second attempt, and the hand check that finds a live driver.

Both are about a report that cannot be read at face value. A run the endpoint
dropped has to be re-attempted in an out-dir of its own and merged back into
the cell it belongs to; a "nothing is running" answer has to come from a check
that has been shown to find something.

The fixtures for a whole experiment directory live next door, in
``test_batch_launcher``, and are imported rather than rebuilt: a second copy
would drift from the one the launcher's own tests exercise.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from pathlib import Path

import pytest
import test_batch_launcher as launcher

from opencollab_eval.commands import batch as batch_cli
from opencollab_eval.experiment import cell_report
from opencollab_eval.experiment.batch_spec import load_spec, spec_digest

EXPERIMENT = launcher.EXPERIMENT
_spec_text = launcher._spec_text

# The same two fixtures the launcher's own tests run on, registered here under
# the names the tests ask for. Bound to the module-level names with an
# underscore so nothing in this file shadows a fixture argument.
_oc_repo = pytest.fixture(name="oc_repo")(launcher.oc_repo.__wrapped__)
_experiment = pytest.fixture(name="experiment")(launcher.experiment.__wrapped__)

# --- the hand check the README tells a human to run --------------------------------------
#
# The README's fallback for "pgrep cannot be trusted here" was
# ``grep -F '[o]pencollab_eval...'``. Under ``-F`` the brackets are literal, so
# the pattern matches no driver that ever ran -- only the grep's own command
# line, which contains it. Run on gpu3 on 2026-09-05 it returned lines and
# none of them was the driver. A check that answers with the wrong process is
# worse than one that errors: the caller reads "something is running" or
# "nothing is running" off a list that never looked.

BATCHES_README = EXPERIMENT / "batches" / "README.md"
PLANTED_ARGV0 = "python -m opencollab_eval.generation.gen_prediction_batch --out-dir planted-positive-control"


def _readme_process_checks() -> list[str]:
    """Every inline command in the batches README that greps ``ps`` output."""
    text = BATCHES_README.read_text(encoding="utf-8")
    return [
        code.replace("\\|", "|")  # the table escapes the pipe
        for code in re.findall(r"`([^`\n]+)`", text)
        if code.startswith("ps -eo") and "gen_prediction_batch" in code
    ]


@pytest.fixture
def planted_driver():
    """A process whose command line is a driver's, and nothing else about it."""
    proc = subprocess.Popen(["bash", "-c", f"exec -a {shlex.quote(PLANTED_ARGV0)} sleep 30"])
    for _ in range(100):
        listing = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True, check=True).stdout
        if any(str(proc.pid) == line.split(maxsplit=1)[0] and "gen_prediction_batch" in line
               for line in listing.splitlines()[1:]):
            break
        time.sleep(0.05)
    else:  # pragma: no cover - the control itself failed
        proc.kill()
        pytest.fail("the positive control never appeared in ps; the test below would prove nothing")
    yield proc
    proc.kill()
    proc.wait()


def test_the_readme_hand_check_sees_a_planted_driver(planted_driver) -> None:
    checks = _readme_process_checks()
    assert checks, f"{BATCHES_README} documents no hand check for a running driver"
    for command in checks:
        out = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False).stdout
        found = [line for line in out.splitlines() if str(planted_driver.pid) == line.split(maxsplit=1)[0]]
        assert found, f"{command!r} did not see the planted driver; it returned {out!r}"
        # And it must not answer with itself: the grep's own command line
        # carries the pattern, which is the whole reason the bracket is there.
        # Matched on the command, not on the whole line: any shell that happens
        # to hold the pattern in an argument is ambient noise, the grep process
        # is the defect.
        itself = [
            line for line in out.splitlines()
            if len(line.split(maxsplit=2)) == 3 and line.split(maxsplit=2)[2].split()[0].endswith("grep")
        ]
        assert not itself, f"{command!r} matched its own grep ({itself}); its answers are not process counts"


def test_the_readme_hand_check_carries_its_own_positive_control() -> None:
    """The instruction to plant a process before believing a "nothing running".

    An empty result has two causes and the reader cannot tell them apart, so
    the README has to say how to make the check hit something it should hit.
    """
    text = BATCHES_README.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if "sees a planted process" in line)
    assert "exec -a" in row and "sleep" in row, row

# --- retries: a second attempt at the instances an endpoint dropped -----------------------

#: The digest of a checked-in spec, frozen. ``retry_of`` had to enter the batch
#: identity, and a new key with a ``None`` value would have changed the digest of
#: every spec already on disk -- which is the pre-flight's own test for "this
#: out-dir holds a different batch". This pins the answer for a spec that names
#: no retry, so the field can only ever change the identity of a spec that uses it.
CMDPLAIN30_DIGEST = "4fb8ac66259c434e9db5ae9649627ef063bf70e031cdd1b9c7a0c8513df30caa"


def test_retry_of_changes_the_identity_only_for_the_specs_that_use_it(experiment: dict) -> None:
    from opencollab_eval.experiment.batch_spec import spec_identity

    assert spec_digest(load_spec(EXPERIMENT / "batches" / "cmdplain30.yaml")) == CMDPLAIN30_DIGEST
    base = load_spec(experiment["spec"])
    assert "retry_of" not in spec_identity(base)
    path = experiment["dir"] / "batches" / "t1r.yaml"
    path.write_text(
        _spec_text(experiment, "name: t1", "name: t1r\nretry_of: t1").replace(
            "rows: {start: 1, stop: 2}", "rows: {start: 2, stop: 2}"
        ),
        encoding="utf-8",
    )
    retry = load_spec(path)
    assert retry.retry_of == "t1"
    assert spec_identity(retry)["retry_of"] == "t1"
    assert spec_digest(retry) != spec_digest(base)


def _plan(experiment: dict, path: Path) -> int:
    return batch_cli.main(
        ["--experiment-dir", str(experiment["dir"]), "plan", str(path)], remote_factory=lambda h: None
    )


def _retry_spec(experiment: dict, name: str, rows: str, edit: tuple[str, str] | None = None) -> Path:
    text = _spec_text(experiment, "name: t1", f"name: {name}\nretry_of: t1").replace(
        "rows: {start: 1, stop: 2}", rows
    )
    if edit is not None:
        assert edit[0] in text, edit[0]
        text = text.replace(*edit)
    path = experiment["dir"] / "batches" / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_plan_refuses_a_retry_whose_original_was_never_planned(experiment: dict, capsys) -> None:
    path = _retry_spec(experiment, "t1r", "rows: {start: 2, stop: 2}")
    assert _plan(experiment, path) == 2
    assert "retry_of" in capsys.readouterr().err


def test_plan_refuses_a_retry_that_changes_a_paid_field(experiment: dict, capsys) -> None:
    assert _plan(experiment, Path(experiment["spec"])) == 0  # the original's record
    path = _retry_spec(
        experiment, "t1r", "rows: {start: 2, stop: 2}", ("budget_per_seat: 2000000", "budget_per_seat: 1000000")
    )
    assert _plan(experiment, path) == 2
    err = capsys.readouterr().err
    assert "budget_per_seat" in err


def test_plan_refuses_a_retry_outside_the_original_slice(experiment: dict, capsys) -> None:
    assert _plan(experiment, Path(experiment["spec"])) == 0
    path = _retry_spec(experiment, "t1r", "rows: {start: 3, stop: 3}")
    assert _plan(experiment, path) == 2
    assert "c__c-3" in capsys.readouterr().err


def test_plan_accepts_a_retry_of_one_row_of_the_original(experiment: dict, capsys) -> None:
    assert _plan(experiment, Path(experiment["spec"])) == 0
    path = _retry_spec(experiment, "t1r", "rows: {start: 2, stop: 2}")
    assert _plan(experiment, path) == 0
    assert "retry of t1" in capsys.readouterr().out


def _metrics(cell: Path, rows: list[dict]) -> None:
    cell.mkdir(parents=True, exist_ok=True)
    (cell / "metrics.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_retry_merge_takes_the_last_valid_attempt(tmp_path: Path) -> None:
    first = tmp_path / "b"
    _metrics(
        first,
        [
            {"instance_id": "a", "run_summary": {"status": "completed", "tokens": 10}},
            {"instance_id": "b", "run_summary": {"status": "failed", "reason": "APIError", "tokens": 1}},
            {"instance_id": "c", "run_summary": {"status": "failed", "reason": "APIError", "tokens": 2}},
            {"instance_id": "d", "run_summary": {"status": "completed", "tokens": 4}},
        ],
    )
    second = tmp_path / "b-r"
    _metrics(
        second,
        [
            {"instance_id": "b", "run_summary": {"status": "completed", "tokens": 90}},
            {"instance_id": "c", "run_summary": {"status": "failed", "reason": "APIError", "tokens": 3}},
            {"instance_id": "d", "run_summary": {"status": "completed", "tokens": 40}},
        ],
    )
    rows = cell_report.merge_attempts(
        [("b", cell_report.run_rows(first, "single")), ("b-r", cell_report.run_rows(second, "single"))]
    )
    by_id = {r.instance_id: r for r in rows}
    assert sorted(by_id) == ["a", "b", "c", "d"]
    assert (by_id["a"].attempt, by_id["a"].attempts, by_id["a"].source_batch) == (1, 1, "b")
    # b's second attempt is the one that ran: the failed first attempt is dropped.
    assert (by_id["b"].attempt, by_id["b"].attempts, by_id["b"].source_batch) == (2, 2, "b-r")
    assert by_id["b"].tokens == 90
    # c never ran: the last attempt is kept so the instance is not silently gone.
    assert (by_id["c"].attempt, by_id["c"].attempts, by_id["c"].source_batch) == (2, 2, "b-r")
    assert by_id["c"].tokens == 3 and not by_id["c"].valid
    # Both attempts at d ran. The rule is the LAST that ran, not the first.
    assert (by_id["d"].attempt, by_id["d"].attempts, by_id["d"].source_batch) == (2, 2, "b-r")
    assert by_id["d"].tokens == 40
    summary = cell_report.summarize(rows, team=False)
    assert summary["retried"] == ["b", "c", "d"] and summary["retried_count"] == 3
    assert summary["retry_succeeded"] == ["b", "d"] and summary["retry_succeeded_count"] == 2
    assert summary["infra_failed"] == ["c"] and summary["infra_failed_count"] == 1
    assert "attempt 2 of 2" in cell_report.render(rows, summary, [])


def test_a_report_without_a_retry_names_every_run_attempt_one(tmp_path: Path) -> None:
    cell = tmp_path / "b"
    _metrics(
        cell,
        [
            {"instance_id": "a", "run_summary": {"status": "completed", "tokens": 10}},
            {"instance_id": "b", "run_summary": {"status": "failed", "reason": "APIError", "tokens": 1}},
        ],
    )
    rows = cell_report.merge_attempts([("b", cell_report.run_rows(cell, "single"))])
    assert [(r.attempt, r.attempts, r.source_batch) for r in rows] == [(1, 1, "b"), (1, 1, "b")]
    summary = cell_report.summarize(rows, team=False)
    assert summary["retried"] == [] and summary["retry_succeeded"] == []
    # One attempt that never ran is still an instance the endpoint took away.
    assert summary["infra_failed"] == ["b"]
    assert "attempt" not in cell_report.render(rows, summary, [])
