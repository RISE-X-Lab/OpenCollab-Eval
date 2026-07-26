from __future__ import annotations

import hashlib

import pytest
from swe_final_report_test_support import fake_compile, final_report_args, mutate_official_report

from opencollab_eval.commands import swe_final_report
from opencollab_eval.commands import swe_final_report_dataset as final_report_dataset
from opencollab_eval.commands.swe_final_report_model import FinalReportInputError


@pytest.fixture(autouse=True)
def _trust_synthetic_dataset(monkeypatch):
    monkeypatch.setattr(
        final_report_dataset,
        "_trusted_dataset_sha256",
        lambda raw: hashlib.sha256(raw).hexdigest(),
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("eval_patch_sha256", "f" * 64, "mismatched eval_patch_sha256"),
        ("filtered_patch_paths", ["tests/test_a.py"], "filtered_patch_paths"),
    ],
)
def test_final_report_rejects_stale_projection_report(tmp_path, monkeypatch, field, value, message):
    args = final_report_args(tmp_path)
    mutate_official_report(
        args.method_a_audit_manifest,
        lambda report: report["task-1"].__setitem__(field, value),
    )
    monkeypatch.setattr(swe_final_report, "_compile_pdf", fake_compile)

    with pytest.raises(FinalReportInputError, match=message):
        swe_final_report.run_from_args(args)
