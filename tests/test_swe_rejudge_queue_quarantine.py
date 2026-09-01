from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from test_swe_rejudge_queue_reliability import (
    _accept_terminal,
    _plan,
    _terminal_report,
)

from opencollab_eval.commands import swe_rejudge_queue as queue
from opencollab_eval.commands.swe_v1_prolite_report import (
    eval_only_reconciliation_reports,
)


def _seed_identity_summary(parent: Path, job: dict[str, object]) -> None:
    parent.joinpath("parallel_summary.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "index": job["index"],
                        "task": job["task"],
                        "generation": {
                            "record_id": job["record_id"],
                            "patch_sha256": job["source_patch_sha256"],
                            "source_patch_sha256": job["source_patch_sha256"],
                            "eval_patch_sha256": job["eval_patch_sha256"],
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_replace_failure_keeps_failed_child_out_of_future_terminal_scan(
    tmp_path, monkeypatch
):
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    job = queue._read_plan(plan)["jobs"][0]
    _seed_identity_summary(parent, job)
    child: list[Path] = []

    def fake_run(argv, *, log, timeout):
        del log, timeout
        output = Path(argv[argv.index("--json-output") + 1])
        child.append(output)
        _terminal_report(output, index=25, patch_sha256="a" * 64, resolved=True)
        return SimpleNamespace(returncode=125)

    monkeypatch.setattr(queue, "_run_bounded_child", fake_run)
    original_replace, original_unlink = Path.replace, Path.unlink

    def fail_replace(path, target):
        if child and path == child[0]:
            raise OSError("replace blocked")
        return original_replace(path, target)

    def fail_unlink(path, *args, **kwargs):
        if child and path == child[0]:
            raise OSError("retirement blocked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    result = queue.run_queue(plan, tmp_path / "state", workers=1)

    assert result["counts"] == {"command_failed": 1}
    assert child[0].exists()
    assert list(parent.glob(child[0].name + ".command_failed*"))
    assert queue._terminal_report(job) == (None, "missing")


def test_valid_child_refresh_survives_malformed_retirement_failure(
    tmp_path, monkeypatch
):
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    job = queue._read_plan(plan)["jobs"][0]
    _seed_identity_summary(parent, job)
    malformed = parent / "task_99_eval_only_old.json"
    malformed.write_text("{", encoding="utf-8")
    child: list[Path] = []

    def fake_run(argv, *, log, timeout):
        del log, timeout
        output = Path(argv[argv.index("--json-output") + 1])
        child.append(output)
        _terminal_report(output, index=25, patch_sha256="a" * 64, resolved=True)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(queue, "_run_bounded_child", fake_run)
    original_unlink = Path.unlink

    def fail_unlink(path, *args, **kwargs):
        if path == malformed:
            raise OSError("retirement blocked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    refreshes = []
    monkeypatch.setattr(
        queue,
        "update_parent_fact_report",
        lambda args: refreshes.append(args) or {"status": "done"},
    )
    result = queue.run_queue(plan, tmp_path / "state", workers=1)

    assert result["counts"] == {"terminal": 1}
    assert len(refreshes) == 1
    assert refreshes[0].ignored_reports == ()
    assert queue._has_quarantine_marker(malformed)
    assert eval_only_reconciliation_reports(parent, child[0]) == [child[0]]
