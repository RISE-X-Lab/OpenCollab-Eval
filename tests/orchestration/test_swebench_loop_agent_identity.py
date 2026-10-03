"""Loop report JSON retains the native entry-agent identifier zero."""

import json

import pytest

from opencollab_eval.commands import swebench_loop_monitor as monitor
from tests.orchestration.test_swebench_loop_journal import TEXT, _args, _native_journal


@pytest.mark.xfail(strict=True, reason="P3-09 aid zero is converted to an empty string")
@pytest.mark.parametrize("source", ["snapshot", "events"])
def test_entry_agent_zero_survives_report_json(tmp_path, source):
    if source == "snapshot":
        _native_journal(tmp_path)
    else:
        (tmp_path / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in [
            {"type": "text_delta", "data": {"aid": 0, "content": TEXT}},
            {"type": "step_end", "data": {"aid": 0}},
        ]))
    report = json.loads(json.dumps(monitor.build_report(_args(tmp_path))))
    assert report["input_complete"] is True
    assert report["recent_assistant_texts"]
    assert {text["aid"] for text in report["recent_assistant_texts"]} == {"0"}
    if source == "snapshot":
        assert report["last_successful_write"]["aid"] == "0"
