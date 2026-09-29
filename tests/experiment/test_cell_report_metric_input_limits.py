"""Torn metric rows retain the same memory limits as complete JSONL records."""

import pytest

from opencollab_eval.engine.swe_eval_records import RecordInputLimitError
from opencollab_eval.experiment import cell_report_metric_order


@pytest.mark.parametrize("limit", ["MAX_JSONL_RETAINED_ROWS", "MAX_JSONL_RETAINED_BYTES"])
def test_damaged_rows_count_toward_retained_input_limits(tmp_path, monkeypatch, limit):
    metrics = tmp_path / "metrics.jsonl"
    metrics.write_bytes(b'{"x":\n{"y":\n')
    monkeypatch.setattr(cell_report_metric_order, limit, 1 if limit.endswith("ROWS") else 6)
    with pytest.raises(RecordInputLimitError, match="retained row or byte limit"):
        cell_report_metric_order._read_metrics_with_damaged_lines(metrics)
