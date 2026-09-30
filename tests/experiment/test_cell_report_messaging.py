"""Topology traffic uses scheduler decisions or matching queue receipts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencollab_eval.experiment import cell_report
from opencollab_eval.experiment.cell_report_rows import Seat, _read_seat, walked_edges
from tests.experiment.cell_report_support import _runtime_dir, _seat_file, _topology_log, _write_metrics


def _messages(arguments: dict, receipt: str | None, call_id: str = "call1") -> list[dict]:
    messages = [{
        "role": "assistant",
        "tool_calls": [{"id": "call1", "function": {"name": "message_agent", "arguments": json.dumps(arguments)}}],
    }]
    if receipt is not None:
        messages.append({"role": "tool", "tool_call_id": call_id, "content": receipt})
    return messages


def _snapshot(path: Path, messages: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"aid": 0, "role": "analyst", "messages": messages}))


@pytest.mark.parametrize(
    ("arguments", "receipt", "expected"),
    [
        ({"to_role": "coder", "to_aid": 1}, "Error: give exactly one of to_role or to_aid.", 0),
        ({"to_role": " Coder "}, "Message queued to aid 1. The teammate runs on its next turn.", 1),
        ({"to_aid": 1}, "Error: teammate inbox is full.", 0),
        ({"to_aid": 1}, "Error: scheduler is shutting down.", 0),
        ({"to_aid": 1}, None, 0),
    ],
)
def test_legacy_snapshots_count_only_acknowledged_sends(
    tmp_path: Path, arguments: dict, receipt: str | None, expected: int
) -> None:
    path = tmp_path / "agent_0_analyst.json"
    _snapshot(path, _messages(arguments, receipt))
    aid, seat = _read_seat(path)
    seats = {str(aid): seat}
    if "to_role" not in arguments:
        seats["1"] = Seat(role="coder")
    assert seat.msg_agent == 1
    assert seat.msg_agent_sent == expected
    assert len(walked_edges(seats, {("analyst", "coder")})) == expected


def test_a_receipt_from_another_tool_call_cannot_confirm_the_send(tmp_path: Path) -> None:
    path = tmp_path / "agent_0_analyst.json"
    _snapshot(path, _messages({"to_role": "coder"}, "Message queued to aid 1.", call_id="other-call"))
    _, seat = _read_seat(path)
    assert seat.msg_agent == 1 and seat.msg_agent_sent == 0


@pytest.mark.parametrize("event_type", ["message_sent", "message_refused"])
def test_scheduler_decisions_override_requested_and_receipt_targets(tmp_path: Path, event_type: str) -> None:
    cell = tmp_path / "cell"
    runtime = _runtime_dir(cell, "team", "case-a")
    _snapshot(runtime / "agent_0_analyst.json", _messages({"to_role": "wrong"}, "Message queued to aid 2."))
    _seat_file(runtime, "agent_1_coder.json", aid=1, role="coder", tokens=1, assistant=1)
    _topology_log(runtime, [("analyst", "coder"), ("analyst", "tester")])
    event = {
        "type": event_type,
        "payload": {"from_aid": 0, "to_aid": 1, "to_role": "coder", "message_id": "message-1"},
    }
    with (runtime / "trajectory.jsonl").open("a") as handle:
        # Duplicate trace copies of the same message retain one send.
        handle.write((json.dumps(event) + "\n") * 2)
    _write_metrics(cell, [{"instance_id": "case-a", "run_summary": {"status": "completed"}}])
    rows = cell_report.run_rows(cell, "team")
    seat = rows[0].seats["0"]
    sent = 1 if event_type == "message_sent" else 0
    assert seat.msg_agent == 1 and seat.msg_agent_sent == sent
    assert seat.msg_agent_targets == (["role:coder"] if sent else [])
    assert rows[0].edges_walked == sent
    summary = cell_report.summarize(rows, None)
    assert summary["message_agent_attempts"] == 1 and summary["message_agent_sent"] == sent
    rendered = cell_report.render(rows, summary, [])
    assert "msgA" in rendered and "msgS" in rendered
