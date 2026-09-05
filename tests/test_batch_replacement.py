"""A pre-registered instance whose evaluation environment is unusable.

``pylint-dev__pylint-4661`` is row 39 of ``subset-50``. Its SWE-bench
evaluation environment does not run -- the benchmark's own gold patch does not
resolve there -- so no run of any arm on that instance can be scored, and the
score is the outcome the grid reads. A broken environment is not a random
event, so its replacement cannot be chosen after the fact: the draw carries an
ordered reserve for exactly this, and the replacement is the first row of it
that no cell has run.

That makes two out-dirs one cell again, but the other way round from a retry.
A retry adds a second attempt at an instance the cell keeps; a replacement
takes an instance *out* of every denominator and puts a different one in. The
run rows of the instance that left are not deleted -- a paid run that no
number counts still has to be findable -- so they move to ``excluded`` in the
JSON with the reason they left.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_batch_launcher as launcher
from test_batch_retry import CMDPLAIN30_DIGEST

from opencollab_eval.commands import batch as batch_cli
from opencollab_eval.experiment import cell_report
from opencollab_eval.experiment.batch_spec import SpecError, load_spec, spec_digest, spec_identity

EXPERIMENT = launcher.EXPERIMENT
_spec_text = launcher._spec_text

_oc_repo = pytest.fixture(name="oc_repo")(launcher.oc_repo.__wrapped__)
_experiment = pytest.fixture(name="experiment")(launcher.experiment.__wrapped__)

REASON = "eval environment unusable: replaced by {instance} per ordered draw"


def _plan(experiment: dict, path: Path) -> int:
    return batch_cli.main(
        ["--experiment-dir", str(experiment["dir"]), "plan", str(path)], remote_factory=lambda h: None
    )


def _report(experiment: dict, path: Path, json_out: Path) -> int:
    return batch_cli.main(
        [
            "--experiment-dir", str(experiment["dir"]), "report", str(path),
            "--scanner", "none", "--json", str(json_out),
        ],
        remote_factory=lambda h: None,
    )


def _replacement_spec(
    experiment: dict,
    name: str,
    rows: str,
    instance: str,
    *,
    batch: str = "t1",
    edit: tuple[str, str] | None = None,
) -> Path:
    text = _spec_text(
        experiment, "name: t1", f"name: {name}\nreplaces: {{batch: {batch}, instance: {instance}}}"
    ).replace("rows: {start: 1, stop: 2}", rows)
    if edit is not None:
        assert edit[0] in text, edit[0]
        text = text.replace(*edit)
    path = experiment["dir"] / "batches" / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _retry_spec(experiment: dict, name: str, of: str, rows: str, suite: str) -> Path:
    text = (
        _spec_text(experiment, "name: t1", f"name: {name}\nretry_of: {of}")
        .replace("rows: {start: 1, stop: 2}", rows)
        .replace("suite: tiny\n", f"suite: {suite}\n")
    )
    path = experiment["dir"] / "batches" / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _metrics(cell: Path, rows: list[dict]) -> None:
    cell.mkdir(parents=True, exist_ok=True)
    (cell / "metrics.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _run(instance: str, status: str = "completed", tokens: int = 10, reason: str = "") -> dict:
    summary = {"status": status, "tokens": tokens}
    if reason:
        summary["reason"] = reason
    return {"instance_id": instance, "run_summary": summary}


# --- the spec field ---------------------------------------------------------------


def test_replaces_changes_the_identity_only_for_the_specs_that_use_it(experiment: dict) -> None:
    """The same rule ``retry_of`` had to obey: a new key must not move an old digest.

    ``spec_digest`` is how the pre-flight decides whether an out-dir holds this
    batch. A key written into the identity unconditionally would change the
    digest of every spec already launched, and every finished batch would read
    as a different batch on resume.
    """
    assert spec_digest(load_spec(EXPERIMENT / "batches" / "cmdplain30.yaml")) == CMDPLAIN30_DIGEST
    base = load_spec(experiment["spec"])
    assert "replaces" not in spec_identity(base)
    path = _replacement_spec(experiment, "t1x", "rows: {start: 3, stop: 3}", "b__b-2")
    spec = load_spec(path)
    assert spec.replaces == {"batch": "t1", "instance": "b__b-2"}
    assert spec_identity(spec)["replaces"] == {"batch": "t1", "instance": "b__b-2"}
    assert spec_digest(spec) != spec_digest(base)


def test_replaces_must_name_a_batch_and_an_instance(experiment: dict) -> None:
    path = experiment["dir"] / "batches" / "bad.yaml"
    path.write_text(_spec_text(experiment, "name: t1", "name: bad\nreplaces: {batch: t1}"), encoding="utf-8")
    with pytest.raises(SpecError, match="replaces"):
        load_spec(path)


def test_replaces_may_not_name_this_batch_itself(experiment: dict) -> None:
    path = _replacement_spec(experiment, "t1x", "rows: {start: 3, stop: 3}", "b__b-2", batch="t1x")
    with pytest.raises(SpecError, match="own name"):
        load_spec(path)


def test_a_spec_is_either_a_retry_or_a_replacement(experiment: dict) -> None:
    """A retry of a replacement batch is a retry: it must not be merged twice.

    ``replaces`` is what the replacement merge looks for. A spec carrying both
    would be folded in once as the replacement's second attempt and once as a
    second replacement of the same instance.
    """
    path = experiment["dir"] / "batches" / "bad.yaml"
    path.write_text(
        _spec_text(experiment, "name: t1", "name: bad\nretry_of: t1\nreplaces: {batch: t1, instance: b__b-2}"),
        encoding="utf-8",
    )
    with pytest.raises(SpecError, match="never both"):
        load_spec(path)


# --- what plan refuses ------------------------------------------------------------


def test_plan_refuses_a_replacement_whose_original_was_never_planned(experiment: dict, capsys) -> None:
    path = _replacement_spec(experiment, "t1x", "rows: {start: 3, stop: 3}", "b__b-2")
    assert _plan(experiment, path) == 2
    assert "replaces" in capsys.readouterr().err


def test_plan_refuses_a_replacement_that_changes_a_paid_field(experiment: dict, capsys) -> None:
    assert _plan(experiment, Path(experiment["spec"])) == 0
    path = _replacement_spec(
        experiment,
        "t1x",
        "rows: {start: 3, stop: 3}",
        "b__b-2",
        edit=("budget_per_seat: 2000000", "budget_per_seat: 1000000"),
    )
    assert _plan(experiment, path) == 2
    assert "budget_per_seat" in capsys.readouterr().err


def test_plan_refuses_a_replacement_of_more_than_one_instance(experiment: dict, capsys) -> None:
    """One broken instance is one replacement. A slice would silently regrow the cell."""
    assert _plan(experiment, Path(experiment["spec"])) == 0
    path = _replacement_spec(experiment, "t1x", "rows: {start: 1, stop: 3}", "b__b-2")
    assert _plan(experiment, path) == 2
    assert "exactly one" in capsys.readouterr().err


def test_plan_refuses_replacing_an_instance_the_original_never_ran(experiment: dict, capsys) -> None:
    assert _plan(experiment, Path(experiment["spec"])) == 0
    path = _replacement_spec(experiment, "t1x", "rows: {start: 3, stop: 3}", "c__c-3")
    assert _plan(experiment, path) == 2
    assert "c__c-3" in capsys.readouterr().err


def test_plan_refuses_a_replacement_the_original_already_ran(experiment: dict, capsys) -> None:
    """Re-running an instance the cell already has is a retry, not a replacement."""
    assert _plan(experiment, Path(experiment["spec"])) == 0
    path = _replacement_spec(experiment, "t1x", "rows: {start: 1, stop: 1}", "b__b-2")
    assert _plan(experiment, path) == 2
    assert "a__a-1" in capsys.readouterr().err


def test_plan_accepts_a_one_row_replacement_from_another_suite(experiment: dict, capsys) -> None:
    """The suite may differ: the reserve row need not live in the file the cell ran."""
    (experiment["dir"] / "suite" / "reserve.csv").write_text(
        "order,instance_id,repo,difficulty,image\n1,c__c-3,c/c,>4 hours,img/c-3:latest\n", encoding="utf-8"
    )
    assert _plan(experiment, Path(experiment["spec"])) == 0
    path = _replacement_spec(
        experiment, "t1x", "rows: {start: 1, stop: 1}", "b__b-2", edit=("suite: tiny\n", "suite: reserve\n")
    )
    assert _plan(experiment, path) == 0
    assert "replaces b__b-2 of t1" in capsys.readouterr().out


# --- what report does with it -----------------------------------------------------


def test_report_drops_the_replaced_instance_and_merges_the_replacement(
    experiment: dict, tmp_path: Path, capsys
) -> None:
    assert _plan(experiment, Path(experiment["spec"])) == 0
    path = _replacement_spec(experiment, "t1x", "rows: {start: 3, stop: 3}", "b__b-2")
    assert _plan(experiment, path) == 0
    root = tmp_path / "batches"
    _metrics(root / "t1", [_run("a__a-1", tokens=10), _run("b__b-2", tokens=20)])
    _metrics(root / "t1x", [_run("c__c-3", tokens=30)])

    out = tmp_path / "r.json"
    assert _report(experiment, Path(experiment["spec"]), out) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))

    assert [r["instance_id"] for r in doc["runs"]] == ["a__a-1", "c__c-3"]
    assert doc["summary"]["runs"] == 2 and doc["summary"]["valid"] == 2
    assert doc["summary"]["replaced"] == ["b__b-2"] and doc["summary"]["replaced_count"] == 1
    # The paid run that left every denominator is still in the document.
    assert [e["instance_id"] for e in doc["excluded"]] == ["b__b-2"]
    assert doc["excluded"][0]["excluded_reason"] == REASON.format(instance="c__c-3")
    assert doc["excluded"][0]["tokens"] == 20
    row = next(r for r in doc["runs"] if r["instance_id"] == "c__c-3")
    assert row["replacement_for"] == "b__b-2" and row["source_batch"] == "t1x" and row["attempt"] == 1
    printed = capsys.readouterr().out
    assert "merged replacement: t1x" in printed
    assert "b__b-2" in printed and "c__c-3" in printed


def test_a_replacement_run_can_itself_be_retried(experiment: dict, tmp_path: Path) -> None:
    """The two mechanisms stack: the replacement is a batch like any other."""
    assert _plan(experiment, Path(experiment["spec"])) == 0
    assert _plan(experiment, _replacement_spec(experiment, "t1x", "rows: {start: 3, stop: 3}", "b__b-2")) == 0
    assert _plan(experiment, _retry_spec(experiment, "t1xr", "t1x", "rows: {start: 3, stop: 3}", "tiny")) == 0
    root = tmp_path / "batches"
    _metrics(root / "t1", [_run("a__a-1", tokens=10), _run("b__b-2", tokens=20)])
    _metrics(root / "t1x", [_run("c__c-3", status="failed", tokens=1, reason="APIError")])
    _metrics(root / "t1xr", [_run("c__c-3", tokens=90)])

    out = tmp_path / "r.json"
    assert _report(experiment, Path(experiment["spec"]), out) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    row = next(r for r in doc["runs"] if r["instance_id"] == "c__c-3")
    assert (row["attempt"], row["attempts"], row["source_batch"]) == (2, 2, "t1xr")
    assert row["tokens"] == 90 and row["replacement_for"] == "b__b-2"
    assert doc["summary"]["valid"] == 2 and doc["summary"]["replaced"] == ["b__b-2"]
    assert doc["summary"]["retried"] == ["c__c-3"]


def test_a_replacement_that_was_never_pulled_leaves_the_paid_run_in_place(
    experiment: dict, tmp_path: Path, capsys
) -> None:
    """The positive control on the drop: nothing leaves a denominator for nothing.

    Dropping the replaced instance the moment a replacement spec exists would
    shrink the cell by one before the replacement had run.
    """
    assert _plan(experiment, Path(experiment["spec"])) == 0
    assert _plan(experiment, _replacement_spec(experiment, "t1x", "rows: {start: 3, stop: 3}", "b__b-2")) == 0
    root = tmp_path / "batches"
    _metrics(root / "t1", [_run("a__a-1", tokens=10), _run("b__b-2", tokens=20)])

    out = tmp_path / "r.json"
    assert _report(experiment, Path(experiment["spec"]), out) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert [r["instance_id"] for r in doc["runs"]] == ["a__a-1", "b__b-2"]
    assert doc["summary"]["replaced"] == [] and doc["excluded"] == []
    assert "not merged" in capsys.readouterr().out


def test_a_cell_with_no_replacement_carries_an_empty_ledger(experiment: dict, tmp_path: Path) -> None:
    assert _plan(experiment, Path(experiment["spec"])) == 0
    root = tmp_path / "batches"
    _metrics(root / "t1", [_run("a__a-1", tokens=10), _run("b__b-2", tokens=20)])
    out = tmp_path / "r.json"
    assert _report(experiment, Path(experiment["spec"]), out) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["summary"]["replaced"] == [] and doc["summary"]["replaced_count"] == 0
    assert doc["excluded"] == []
    rows = cell_report.run_rows(root / "t1", "team")
    assert "replaced" not in cell_report.render(rows, cell_report.summarize(rows), [])
