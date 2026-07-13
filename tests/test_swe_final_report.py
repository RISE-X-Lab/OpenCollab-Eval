from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab_eval.commands import swe_final_report
from opencollab_eval.commands.swe_final_report_model import FinalReportInputError


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _write_json(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _fact_report(path: Path, *, name: str, resolved: set[int]) -> Path:
    tasks = []
    for index in range(1, 101):
        verdict = index in resolved
        tasks.append(
            {
                "index": index,
                "task": f"task-{index}",
                "generation_status": "generation_done",
                "eval_status": "eval_done",
                "eval_success": True,
                "eval_pending": False,
                "resolved": verdict,
                "technical_failed": False,
                "technical_reasons": [],
                "record_id": f"record-{name}-{index}",
                "patch_sha256": _sha(f"{name}-{index}"),
                "direct_execution_proven": True,
                "report_path": f"/reports/{name}/{index}.json",
                "attempt_count": 1,
                "eval_attempt_count": 1,
            }
        )
    return _write_json(
        path,
        {
            "schema": "opencollab.swe_eval_layer_final_report.v1",
            "expected_indices": list(range(1, 101)),
            "census_errors": [],
            "counts": {
                "tasks": 100,
                "eval_success": 100,
                "eval_pending": 0,
                "eval_failed": 0,
                "empty_patch": 0,
                "over_budget_tasks": 0,
                "resolved": len(resolved),
                "unresolved": 100 - len(resolved),
                "technical_failed_final": 0,
            },
            "tasks": tasks,
        },
    )


def _audit_manifest(path: Path, *, method: str, report: Path, resolved: set[int], dataset_sha: str) -> Path:
    evidence = path.with_name(f"{path.stem}.evidence.json")
    report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
    runtime = {
        "opencollab_commit": "a" * 40,
        "opencollab_eval_commit": "b" * 40,
        "dataset_sha256": dataset_sha,
    }
    fact_tasks = json.loads(report.read_text(encoding="utf-8"))["tasks"]
    evidence_tasks = [
        {
            "index": task["index"],
            "task": task["task"],
            "record_id": task["record_id"],
            "patch_sha256": task["patch_sha256"],
            "trajectory_clean": True,
            "candidate_identity_verified": True,
            "network_isolated": True,
            "direct_execution_proven": True,
        }
        for task in fact_tasks
    ]
    _write_json(
        evidence,
        {
            "schema": "opencollab.swe_clean_run_evidence.v1",
            "method": method,
            "source_report_sha256": report_sha,
            "runtime": runtime,
            "covered_indices": list(range(1, 101)),
            "clean_trajectory_indices": list(range(1, 101)),
            "candidate_identity_indices": list(range(1, 101)),
            "network_isolation_indices": list(range(1, 101)),
            "direct_execution_indices": list(range(1, 101)),
            "resolved_execution_indices": sorted(resolved),
            "tasks": evidence_tasks,
        },
    )
    evidence_sha = hashlib.sha256(evidence.read_bytes()).hexdigest()
    return _write_json(
        path,
        {
            "schema": "opencollab.swe_clean_run_manifest.v1",
            "method": method,
            "source_report_sha256": report_sha,
            "expected_indices": list(range(1, 101)),
            "clean_trajectory_indices": list(range(1, 101)),
            "candidate_identity_indices": list(range(1, 101)),
            "network_isolation_indices": list(range(1, 101)),
            "direct_execution_indices": list(range(1, 101)),
            "resolved_execution_indices": sorted(resolved),
            "runtime": runtime,
            "evidence_files": [{"path": evidence.name, "sha256": evidence_sha}],
        },
    )


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    resolved_a = {7, 11, 21, 32, 34, 35}
    resolved_b = {7, 11, 21, 32, 33}
    report_a = _fact_report(tmp_path / "a.json", name="A", resolved=resolved_a)
    report_b = _fact_report(tmp_path / "b.json", name="B", resolved=resolved_b)
    dataset_sha = "c" * 64
    audit_a = _audit_manifest(
        tmp_path / "a-audit.json",
        method="Method-A",
        report=report_a,
        resolved=resolved_a,
        dataset_sha=dataset_sha,
    )
    audit_b = _audit_manifest(
        tmp_path / "b-audit.json",
        method="Method-B",
        report=report_b,
        resolved=resolved_b,
        dataset_sha=dataset_sha,
    )
    return report_a, report_b, audit_a, audit_b


def _mutate_audit_evidence(manifest_path: Path, mutate) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    evidence_path = manifest_path.parent / manifest["evidence_files"][0]["path"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    mutate(manifest, evidence)
    _write_json(evidence_path, evidence)
    manifest["evidence_files"][0]["sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    _write_json(manifest_path, manifest)


def _args(tmp_path: Path) -> SimpleNamespace:
    report_a, report_b, audit_a, audit_b = _inputs(tmp_path)
    return SimpleNamespace(
        method_a_report=report_a,
        method_a_audit_manifest=audit_a,
        method_a_name="Method-A",
        method_b_report=report_b,
        method_b_audit_manifest=audit_b,
        method_b_name="Method-B",
        dataset="swe-batch-pro-lite",
        meeting_date="2026-07-15",
        author="A&B_#1",
        narrative_json=None,
        labels_json=None,
        output_dir=tmp_path / "output",
        output_prefix="comparison",
        latex_engine="xelatex",
        latex_timeout=30.0,
    )


def _fake_compile(tex_path: Path, *, output_dir: Path, latex_engine: str, timeout_seconds: float):
    assert latex_engine == "xelatex"
    assert timeout_seconds == 30.0
    pdf = output_dir / f"{tex_path.stem}.pdf"
    pdf.write_bytes(b"%PDF-1.7\n" + b"x" * 2048)
    return pdf, "ok"


def test_final_report_publishes_all_formats_from_one_model(tmp_path, monkeypatch):
    args = _args(tmp_path)
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    publication = swe_final_report.run_from_args(args)

    assert publication["status"] == "final"
    manifest = json.loads((args.output_dir / "comparison.manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "final"
    assert set(manifest["outputs"]) == {"json", "markdown", "tex", "pdf"}
    model = json.loads((args.output_dir / "comparison.json").read_text(encoding="utf-8"))
    assert model["methods"] == {"method_a": "Method-A", "method_b": "Method-B"}
    assert model["counts"]["method_a"]["resolved"] == 6
    assert model["counts"]["method_b"]["resolved"] == 5
    assert model["indices"]["common_resolved"] == [7, 11, 21, 32]
    assert model["indices"]["only_method_a_resolved"] == [34, 35]
    assert model["indices"]["only_method_b_resolved"] == [33]
    assert len(model["indices"]["neither_resolved"]) == 93
    assert len(model["tasks"]) == 100
    markdown = (args.output_dir / "comparison.md").read_text(encoding="utf-8")
    tex = (args.output_dir / "comparison.tex").read_text(encoding="utf-8")
    assert "| 33 | unresolved | resolved |" in markdown
    assert "A\\&B\\_\\#1" in tex
    assert "\\allowbreak{}" in tex


@pytest.mark.parametrize("method_name", ["bad/name", "方法甲"])
def test_final_report_rejects_method_names_that_break_the_output_schema(tmp_path, method_name):
    args = _args(tmp_path)
    args.method_a_name = method_name

    with pytest.raises(FinalReportInputError, match="method name is unsafe"):
        swe_final_report.run_from_args(args)


def test_final_report_rejects_a_dataset_that_disagrees_with_the_fixed_census(tmp_path):
    args = _args(tmp_path)
    args.dataset = "fake-benchmark"

    with pytest.raises(FinalReportInputError, match="dataset must be swe-batch-pro-lite"):
        swe_final_report.run_from_args(args)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda report: report["tasks"].pop(), "100 task rows"),
        (lambda report: report["tasks"].__setitem__(1, dict(report["tasks"][0])), "duplicated"),
        (lambda report: report["tasks"][0].__setitem__("eval_pending", True), "still pending"),
        (lambda report: report["tasks"][0].__setitem__("technical_failed", True), "is technical"),
        (lambda report: report["tasks"][0].__setitem__("resolved", None), "Boolean verdict"),
        (lambda report: report["tasks"][0].__setitem__("direct_execution_proven", False), "execution proof"),
        (lambda report: report["tasks"][0].__setitem__("patch_sha256", "short"), "patch SHA-256"),
        (lambda report: report["tasks"][1].__setitem__("record_id", report["tasks"][0]["record_id"]), "duplicated"),
    ],
)
def test_final_report_rejects_non_terminal_or_ambiguous_fact_rows(tmp_path, monkeypatch, mutate, message):
    args = _args(tmp_path)
    report = json.loads(args.method_a_report.read_text(encoding="utf-8"))
    mutate(report)
    _write_json(args.method_a_report, report)
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match=message):
        swe_final_report.run_from_args(args)

    manifest = json.loads((args.output_dir / "comparison.manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"


def test_final_report_rejects_an_audit_bound_to_an_old_fact_report(tmp_path, monkeypatch):
    args = _args(tmp_path)
    report = json.loads(args.method_a_report.read_text(encoding="utf-8"))
    report["generated_at"] = "later"
    _write_json(args.method_a_report, report)
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="different fact report"):
        swe_final_report.run_from_args(args)


def test_final_report_rejects_changed_audit_evidence(tmp_path, monkeypatch):
    args = _args(tmp_path)
    evidence = args.method_a_audit_manifest.with_name(f"{args.method_a_audit_manifest.stem}.evidence.json")
    evidence.write_text("changed", encoding="utf-8")
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="not valid JSON|evidence hash changed"):
        swe_final_report.run_from_args(args)


def test_final_report_rejects_different_dataset_identities(tmp_path, monkeypatch):
    args = _args(tmp_path)
    _mutate_audit_evidence(
        args.method_b_audit_manifest,
        lambda manifest, evidence: (
            manifest["runtime"].__setitem__("dataset_sha256", "d" * 64),
            evidence["runtime"].__setitem__("dataset_sha256", "d" * 64),
        ),
    )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="different dataset_sha256"):
        swe_final_report.run_from_args(args)


def test_final_report_rejects_different_task_mappings_between_methods(tmp_path, monkeypatch):
    args = _args(tmp_path)
    report = json.loads(args.method_b_report.read_text(encoding="utf-8"))
    report["tasks"][0]["task"] = "different-task"
    _write_json(args.method_b_report, report)
    report_sha = hashlib.sha256(args.method_b_report.read_bytes()).hexdigest()
    _mutate_audit_evidence(
        args.method_b_audit_manifest,
        lambda manifest, evidence: (
            manifest.__setitem__("source_report_sha256", report_sha),
            evidence.__setitem__("source_report_sha256", report_sha),
            evidence["tasks"][0].__setitem__("task", "different-task"),
        ),
    )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="different tasks"):
        swe_final_report.run_from_args(args)


def test_final_report_escapes_external_narrative_and_labels(tmp_path, monkeypatch):
    args = _args(tmp_path)
    args.narrative_json = _write_json(
        tmp_path / "narrative.json",
        {
            "schema": "opencollab.swe_final_report_narrative.v1",
            "overview": ["Summary with x_y & 50% evidence."],
            "task_notes": [
                {
                    "indices": [7, 11],
                    "title": "Target #1",
                    "text": "Literal {value} and $token.",
                    "evidence_refs": ["a-audit.evidence.json"],
                }
            ],
        },
    )
    args.labels_json = _write_json(
        tmp_path / "labels.json",
        {
            "schema": "opencollab.swe_final_report_labels.v1",
            "labels": {"title": "$method_a & $method_b: 100% final"},
        },
    )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    swe_final_report.run_from_args(args)

    tex = (args.output_dir / "comparison.tex").read_text(encoding="utf-8")
    assert "Method-A \\& Method-B: 100\\% final" in tex
    assert "x\\_y \\& 50\\% evidence" in tex
    assert "Literal \\{value\\} and \\$token" in tex


def test_final_report_safely_renders_spaces_and_backticks_in_evidence_paths(tmp_path, monkeypatch):
    args = _args(tmp_path)
    manifest = json.loads(args.method_a_audit_manifest.read_text(encoding="utf-8"))
    old_evidence = args.method_a_audit_manifest.with_name(f"{args.method_a_audit_manifest.stem}.evidence.json")
    evidence = tmp_path / "evidence with `tick`.json"
    evidence.write_bytes(old_evidence.read_bytes())
    manifest["evidence_files"] = [{"path": evidence.name, "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}]
    _write_json(args.method_a_audit_manifest, manifest)
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    swe_final_report.run_from_args(args)

    markdown = (args.output_dir / "comparison.md").read_text(encoding="utf-8")
    tex = (args.output_dir / "comparison.tex").read_text(encoding="utf-8")
    assert "<code>evidence with `tick`.json</code>" in markdown
    assert "evidence with `tick`" in tex


def test_final_report_method_names_cannot_collide_with_comparison_keys(tmp_path, monkeypatch):
    args = _args(tmp_path)
    args.method_a_name = "A"
    args.method_b_name = "only_A"
    for manifest_path, method in (
        (args.method_a_audit_manifest, "A"),
        (args.method_b_audit_manifest, "only_A"),
    ):
        _mutate_audit_evidence(
            manifest_path,
            lambda manifest, evidence, method=method: (
                manifest.__setitem__("method", method),
                evidence.__setitem__("method", method),
            ),
        )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    swe_final_report.run_from_args(args)

    model = json.loads((args.output_dir / "comparison.json").read_text(encoding="utf-8"))
    assert model["methods"] == {"method_a": "A", "method_b": "only_A"}
    assert model["indices"]["method_b_resolved"] == [7, 11, 21, 32, 33]
    assert model["indices"]["only_method_a_resolved"] == [34, 35]


def test_final_report_rejects_fact_claim_templates_in_labels(tmp_path, monkeypatch):
    args = _args(tmp_path)
    args.labels_json = _write_json(
        tmp_path / "labels.json",
        {
            "schema": "opencollab.swe_final_report_labels.v1",
            "labels": {"summary": "Method-A resolved 100/100"},
        },
    )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="unknown keys"):
        swe_final_report.run_from_args(args)


def test_final_report_rejects_a_dataset_subtitle_override(tmp_path, monkeypatch):
    args = _args(tmp_path)
    args.labels_json = _write_json(
        tmp_path / "labels.json",
        {
            "schema": "opencollab.swe_final_report_labels.v1",
            "labels": {"subtitle": "Different benchmark"},
        },
    )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="unknown keys"):
        swe_final_report.run_from_args(args)


def test_final_report_escapes_markdown_block_injection_from_narrative(tmp_path, monkeypatch):
    args = _args(tmp_path)
    args.narrative_json = _write_json(
        tmp_path / "narrative.json",
        {
            "schema": "opencollab.swe_final_report_narrative.v1",
            "overview": ["# FORGED HEADING", "- forged item", "1. forged item"],
            "task_notes": [],
        },
    )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    swe_final_report.run_from_args(args)

    markdown = (args.output_dir / "comparison.md").read_text(encoding="utf-8")
    assert "\\# FORGED HEADING" in markdown
    assert "\\- forged item" in markdown
    assert "1\\. forged item" in markdown
    assert "\n# FORGED HEADING" not in markdown


def test_final_report_rejects_unstructured_audit_evidence_even_with_matching_hash(tmp_path, monkeypatch):
    args = _args(tmp_path)
    manifest = json.loads(args.method_a_audit_manifest.read_text(encoding="utf-8"))
    evidence_path = args.method_a_audit_manifest.parent / manifest["evidence_files"][0]["path"]
    _write_json(evidence_path, {})
    manifest["evidence_files"][0]["sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    _write_json(args.method_a_audit_manifest, manifest)
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="unsupported schema"):
        swe_final_report.run_from_args(args)


def test_final_report_rejects_task_evidence_without_network_isolation(tmp_path, monkeypatch):
    args = _args(tmp_path)
    _mutate_audit_evidence(
        args.method_a_audit_manifest,
        lambda _manifest, evidence: evidence["tasks"][0].__setitem__("network_isolated", False),
    )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="does not prove network_isolated"):
        swe_final_report.run_from_args(args)


def test_final_report_rejects_narrative_reference_outside_verified_evidence(tmp_path, monkeypatch):
    args = _args(tmp_path)
    args.narrative_json = _write_json(
        tmp_path / "narrative.json",
        {
            "schema": "opencollab.swe_final_report_narrative.v1",
            "overview": [],
            "task_notes": [
                {
                    "indices": [7],
                    "title": "Claim",
                    "text": "Claim text.",
                    "evidence_refs": ["unverified.json"],
                }
            ],
        },
    )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="outside the verified audit set"):
        swe_final_report.run_from_args(args)


def test_final_report_compile_failure_marks_manifest_failed_and_keeps_old_pdf(tmp_path, monkeypatch):
    args = _args(tmp_path)
    args.output_dir.mkdir()
    old_pdf = args.output_dir / "comparison.pdf"
    old_pdf.write_bytes(b"old-pdf")

    def fail_compile(*args, **kwargs):
        raise FinalReportInputError("compiler failed")

    monkeypatch.setattr(swe_final_report, "_compile_pdf", fail_compile)

    with pytest.raises(FinalReportInputError, match="compiler failed"):
        swe_final_report.run_from_args(args)

    assert old_pdf.read_bytes() == b"old-pdf"
    manifest = json.loads((args.output_dir / "comparison.manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"


def test_final_report_preflights_every_target_before_replacing_old_outputs(tmp_path, monkeypatch):
    args = _args(tmp_path)
    args.output_dir.mkdir()
    old = {
        "pdf": b"old-pdf",
        "tex": b"old-tex",
        "md": b"old-markdown",
    }
    for suffix, payload in old.items():
        (args.output_dir / f"comparison.{suffix}").write_bytes(payload)
    (args.output_dir / "comparison.json").mkdir()
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="not a regular file"):
        swe_final_report.run_from_args(args)

    for suffix, payload in old.items():
        assert (args.output_dir / f"comparison.{suffix}").read_bytes() == payload


def test_final_report_rolls_back_every_output_when_publish_fails_midway(tmp_path, monkeypatch):
    args = _args(tmp_path)
    args.output_dir.mkdir()
    old = {
        "pdf": b"old-pdf",
        "tex": b"old-tex",
        "md": b"old-markdown",
        "json": b"old-json",
    }
    for suffix, payload in old.items():
        (args.output_dir / f"comparison.{suffix}").write_bytes(payload)
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)
    real_publish = swe_final_report._publish
    calls = 0

    def fail_second_publish(staged, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected publish failure")
        real_publish(staged, target)

    monkeypatch.setattr(swe_final_report, "_publish", fail_second_publish)

    with pytest.raises(OSError, match="injected publish failure"):
        swe_final_report.run_from_args(args)

    for suffix, payload in old.items():
        assert (args.output_dir / f"comparison.{suffix}").read_bytes() == payload


def test_final_report_rechecks_published_hashes_and_rolls_back_changes(tmp_path, monkeypatch):
    args = _args(tmp_path)
    args.output_dir.mkdir()
    old_pdf = args.output_dir / "comparison.pdf"
    old_pdf.write_bytes(b"old-pdf")
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)
    real_publish = swe_final_report._publish

    def corrupt_published_pdf(staged, target):
        real_publish(staged, target)
        if target.suffix == ".pdf":
            target.write_bytes(b"%PDF-1.7\n" + b"corrupt" * 300)

    monkeypatch.setattr(swe_final_report, "_publish", corrupt_published_pdf)

    with pytest.raises(FinalReportInputError, match="changed before manifest finalization"):
        swe_final_report.run_from_args(args)

    assert old_pdf.read_bytes() == b"old-pdf"


def test_final_report_rejects_a_concurrent_writer_for_the_same_prefix(tmp_path, monkeypatch):
    args = _args(tmp_path)
    args.output_dir.mkdir()
    lock_path = args.output_dir / ".comparison.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)
    try:
        with pytest.raises(FinalReportInputError, match="another publication is active"):
            swe_final_report.run_from_args(args)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    assert not (args.output_dir / "comparison.manifest.json").exists()


def test_cli_final_report_returns_zero_only_after_publication(tmp_path, monkeypatch, capsys):
    args = _args(tmp_path)
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)
    argv = [
        "--method-a-report",
        str(args.method_a_report),
        "--method-a-audit-manifest",
        str(args.method_a_audit_manifest),
        "--method-a-name",
        args.method_a_name,
        "--method-b-report",
        str(args.method_b_report),
        "--method-b-audit-manifest",
        str(args.method_b_audit_manifest),
        "--method-b-name",
        args.method_b_name,
        "--meeting-date",
        args.meeting_date,
        "--author",
        args.author,
        "--output-dir",
        str(args.output_dir),
        "--output-prefix",
        args.output_prefix,
        "--latex-timeout",
        str(args.latex_timeout),
    ]

    assert swe_final_report.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "final"


def test_cli_final_report_returns_nonzero_for_invalid_input(tmp_path, capsys):
    args = _args(tmp_path)
    args.method_a_report.unlink()
    argv = [
        "--method-a-report",
        str(args.method_a_report),
        "--method-a-audit-manifest",
        str(args.method_a_audit_manifest),
        "--method-b-report",
        str(args.method_b_report),
        "--method-b-audit-manifest",
        str(args.method_b_audit_manifest),
        "--meeting-date",
        args.meeting_date,
        "--author",
        "Tester",
        "--output-dir",
        str(args.output_dir),
    ]

    assert swe_final_report.main(argv) == 2
    assert "missing" in capsys.readouterr().err
