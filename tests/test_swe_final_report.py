from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path

import pytest
from swe_final_report_test_support import (
    fake_compile as _fake_compile,
)
from swe_final_report_test_support import (
    final_report_args as _args,
)
from swe_final_report_test_support import (
    mutate_audit_evidence as _mutate_audit_evidence,
)
from swe_final_report_test_support import (
    mutate_official_report as _mutate_official_report,
)
from swe_final_report_test_support import (
    write_json as _write_json,
)

from opencollab_eval.commands import swe_final_report
from opencollab_eval.commands import swe_final_report_model as final_report_model
from opencollab_eval.commands.swe_final_report_model import FinalReportInputError


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
    assert len(model["integrity"]["method_a"]["supporting_artifacts"]) == 4
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
        (
            lambda report: report["tasks"].__setitem__(1, dict(report["tasks"][0])),
            "trusted dataset census",
        ),
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

    with pytest.raises(FinalReportInputError, match="trusted dataset"):
        swe_final_report.run_from_args(args)


def test_final_report_rejects_different_task_mappings_between_methods(tmp_path, monkeypatch):
    args = _args(tmp_path)
    report = json.loads(args.method_b_report.read_text(encoding="utf-8"))
    report["tasks"][0]["task"] = "different-task"
    official_path = Path(report["tasks"][0]["report_path"])
    official = json.loads(official_path.read_text(encoding="utf-8"))
    official["different-task"] = official.pop("task-1")
    official["different-task"]["instance_id"] = "different-task"
    _write_json(official_path, official)
    official_sha = hashlib.sha256(official_path.read_bytes()).hexdigest()
    _write_json(args.method_b_report, report)
    report_sha = hashlib.sha256(args.method_b_report.read_bytes()).hexdigest()

    def bind_changed_task(manifest, evidence):
        manifest["source_report_sha256"] = report_sha
        evidence["source_report_sha256"] = report_sha
        evidence["tasks"][0]["task"] = "different-task"
        for row in evidence["tasks"]:
            row["artifacts"]["official_report"]["sha256"] = official_sha

    _mutate_audit_evidence(
        args.method_b_audit_manifest,
        bind_changed_task,
    )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="trusted dataset census"):
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


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda _manifest, evidence: evidence["tasks"].pop(), "exact ordered task census"),
        (
            lambda _manifest, evidence: evidence["tasks"].__setitem__(1, dict(evidence["tasks"][0])),
            "duplicates task 1",
        ),
        (lambda _manifest, evidence: evidence.__setitem__("method", "Other"), "bound to another run"),
    ],
)
def test_final_report_rejects_incomplete_duplicate_or_misnamed_task_evidence(
    tmp_path,
    monkeypatch,
    mutate,
    message,
):
    args = _args(tmp_path)
    _mutate_audit_evidence(args.method_a_audit_manifest, mutate)
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match=message):
        swe_final_report.run_from_args(args)


def test_final_report_rejects_fact_report_path_not_bound_by_audit_evidence(tmp_path, monkeypatch):
    args = _args(tmp_path)
    report = json.loads(args.method_a_report.read_text(encoding="utf-8"))
    alternate_report = tmp_path / "alternate-official.json"
    alternate_report.write_bytes(Path(report["tasks"][0]["report_path"]).read_bytes())
    report["tasks"][0]["report_path"] = str(alternate_report)
    _write_json(args.method_a_report, report)
    report_sha = hashlib.sha256(args.method_a_report.read_bytes()).hexdigest()
    _mutate_audit_evidence(
        args.method_a_audit_manifest,
        lambda manifest, evidence: (
            manifest.__setitem__("source_report_sha256", report_sha),
            evidence.__setitem__("source_report_sha256", report_sha),
        ),
    )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="official report path does not match"):
        swe_final_report.run_from_args(args)


def test_final_report_rejects_an_official_report_sha_mismatch(tmp_path, monkeypatch):
    args = _args(tmp_path)
    _mutate_audit_evidence(
        args.method_a_audit_manifest,
        lambda _manifest, evidence: evidence["tasks"][0]["artifacts"]["official_report"].__setitem__(
            "sha256", "f" * 64
        ),
    )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="official_report artifact hash changed"):
        swe_final_report.run_from_args(args)


def test_final_report_rejects_a_missing_official_report(tmp_path, monkeypatch):
    args = _args(tmp_path)
    official_path = _mutate_official_report(args.method_a_audit_manifest, lambda _report: None)
    official_path.unlink()
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="missing, unsafe, or unstable"):
        swe_final_report.run_from_args(args)


def test_final_report_recomputes_direct_execution_from_the_official_report(tmp_path, monkeypatch):
    args = _args(tmp_path)
    _mutate_official_report(
        args.method_a_audit_manifest,
        lambda report: report["task-1"]["tests_status"].__setitem__("fail_to_pass_evidence", []),
    )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="lacks executable target-test proof"):
        swe_final_report.run_from_args(args)


def test_final_report_binds_declared_targets_to_the_trusted_dataset(tmp_path, monkeypatch):
    args = _args(tmp_path)
    _mutate_official_report(
        args.method_a_audit_manifest,
        lambda report: report["task-1"]["tests_status"]["fail_to_pass_plan"].__setitem__(
            "declared_targets", ["tests/forged.py::test_wrong"]
        ),
    )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="targets do not match the trusted dataset"):
        swe_final_report.run_from_args(args)


def test_final_report_requires_an_immutable_evaluation_image_identity(tmp_path, monkeypatch):
    args = _args(tmp_path)
    _mutate_official_report(
        args.method_a_audit_manifest,
        lambda report: report["task-1"].__setitem__("eval_image_id", "mutable:latest"),
    )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="immutable evaluation image identity"):
        swe_final_report.run_from_args(args)


def test_bound_artifact_verification_retains_at_most_one_payload(tmp_path):
    paths = [tmp_path / f"artifact-{index}.bin" for index in range(3)]
    references = []
    for index, path in enumerate(paths):
        path.write_bytes(bytes([index + 1]) * 4096)
        references.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    verified: set[tuple[str, str]] = set()
    payload_cache: dict[tuple[str, str], bytes] = {}

    _raw_path, _sha, payload = final_report_model._read_bound_artifact(
        references[0],
        anchor=tmp_path,
        label="supporting artifact",
        verified=verified,
        payload_cache=payload_cache,
        retain_payload=False,
    )
    assert payload is None
    assert payload_cache == {}

    for reference in references[1:]:
        _raw_path, _sha, payload = final_report_model._read_bound_artifact(
            reference,
            anchor=tmp_path,
            label="official artifact",
            verified=verified,
            payload_cache=payload_cache,
            retain_payload=True,
        )
        assert payload is not None
        assert len(payload_cache) == 1
    assert len(verified) == 3


def test_final_report_rejects_official_report_candidate_identity_mismatch(tmp_path, monkeypatch):
    args = _args(tmp_path)
    _mutate_official_report(
        args.method_a_audit_manifest,
        lambda report: report["task-1"].__setitem__("record_id", "different-record"),
    )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="mismatched record_id"):
        swe_final_report.run_from_args(args)


def test_final_report_rejects_official_report_patch_identity_mismatch(tmp_path, monkeypatch):
    args = _args(tmp_path)
    _mutate_official_report(
        args.method_a_audit_manifest,
        lambda report: report["task-1"].__setitem__("patch_sha256", "f" * 64),
    )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="mismatched patch_sha256"):
        swe_final_report.run_from_args(args)


def test_final_report_rejects_changed_supporting_artifact(tmp_path, monkeypatch):
    args = _args(tmp_path)
    manifest = json.loads(args.method_a_audit_manifest.read_text(encoding="utf-8"))
    evidence_path = args.method_a_audit_manifest.parent / manifest["evidence_files"][0]["path"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    trajectory_path = Path(evidence["tasks"][0]["artifacts"]["trajectory"]["path"])
    trajectory_path.write_text("changed", encoding="utf-8")
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)

    with pytest.raises(FinalReportInputError, match="trajectory artifact hash changed"):
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


def test_final_report_failed_republish_preserves_complete_final_publication(tmp_path, monkeypatch):
    args = _args(tmp_path)
    monkeypatch.setattr(swe_final_report, "_compile_pdf", _fake_compile)
    swe_final_report.run_from_args(args)
    published = {
        path.name: path.read_bytes()
        for path in args.output_dir.iterdir()
        if path.name.startswith("comparison.")
    }
    assert set(published) == {
        "comparison.json",
        "comparison.md",
        "comparison.tex",
        "comparison.pdf",
        "comparison.manifest.json",
    }

    def fail_compile(*args, **kwargs):
        raise FinalReportInputError("compiler failed")

    monkeypatch.setattr(swe_final_report, "_compile_pdf", fail_compile)
    with pytest.raises(FinalReportInputError, match="compiler failed"):
        swe_final_report.run_from_args(args)

    assert {
        path.name: path.read_bytes()
        for path in args.output_dir.iterdir()
        if path.name.startswith("comparison.")
    } == published
    manifest = json.loads((args.output_dir / "comparison.manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "final"


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
        "--dataset-file",
        str(args.dataset_file),
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
        "--dataset-file",
        str(args.dataset_file),
        "--meeting-date",
        args.meeting_date,
        "--author",
        "Tester",
        "--output-dir",
        str(args.output_dir),
    ]

    assert swe_final_report.main(argv) == 2
    assert "missing" in capsys.readouterr().err
