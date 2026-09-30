"""Topology traffic uses scheduler decisions or matching queue receipts."""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import pytest

from opencollab_eval.experiment import cell_report
from opencollab_eval.experiment.cell_report_rows import Seat, _event_log_facts, _read_seat, walked_edges
from tests.experiment.cell_report_support import _runtime_dir, _seat_file, _topology_log, _write_metrics


def _messages(
    arguments: dict, receipt: str | None, call_id: str = "call1", *, request_id: str = "call1"
) -> list[dict]:
    messages = [{
        "role": "assistant",
        "tool_calls": [{"id": request_id, "function": {"name": "message_agent", "arguments": json.dumps(arguments)}}],
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


def _decision(arguments: dict, aid: int, message_id: str, *, event_type: str = "message_sent") -> dict:
    summary = arguments["summary"]
    content = arguments["content"]
    return {
        "type": event_type,
        "payload": {
            "from_aid": 0,
            "to_aid": aid,
            "to_role": {0: "analyst", 1: "coder", 2: "tester"}[aid],
            "message_id": message_id,
            "summary": summary[:240],
            "summary_chars": len(summary),
            "content_chars": len(content),
            "content_bytes": len(content.encode("utf-8")),
        },
    }


def _reported_cell(tmp_path: Path, messages: list[dict], events: list[dict], tail: str = ""):
    cell = tmp_path / "cell"
    runtime = _runtime_dir(cell, "team", "case-a")
    _snapshot(runtime / "agent_0_analyst.json", messages)
    _seat_file(runtime, "agent_1_coder.json", aid=1, role="coder", tokens=1, assistant=1)
    _seat_file(runtime, "agent_2_tester.json", aid=2, role="tester", tokens=1, assistant=1)
    _topology_log(runtime, [("analyst", "coder"), ("analyst", "tester")])
    with (runtime / "trajectory.jsonl").open("a") as handle:
        handle.write("".join(json.dumps(event) + "\n" for event in events) + tail)
    _write_metrics(cell, [{"instance_id": "case-a", "run_summary": {"status": "completed"}}])
    return cell_report.run_rows(cell, "team")


@pytest.mark.parametrize("event_present,receipt_present", [(True, True), (True, False), (False, True)])
def test_successful_decision_and_receipt_count_once(
    tmp_path: Path, event_present: bool, receipt_present: bool
) -> None:
    arguments = {"to_role": " Coder ", "summary": "s" * 300, "content": "é"}
    event = _decision(arguments, 1, "message-1")
    messages = _messages(arguments, "Message queued to aid 1." if receipt_present else None)
    rows = _reported_cell(tmp_path, messages, [event, event] if event_present else [])
    seat = rows[0].seats["0"]
    assert seat.msg_agent == 1 and seat.msg_agent_sent == 1
    assert seat.msg_agent_targets == ["role:coder"]
    assert rows[0].edges_walked == 1
    summary = cell_report.summarize(rows, None)
    assert summary["message_agent_attempts"] == 1 and summary["message_agent_sent"] == 1
    rendered = cell_report.render(rows, summary, [])
    assert "msgA" in rendered and "msgS" in rendered


@pytest.mark.parametrize("tail", ["", '{"type":"message_sent","payload":'])
@pytest.mark.parametrize("success_event_present", [False, True])
def test_partial_refusal_events_retain_later_successful_receipt(
    tmp_path: Path, tail: str, success_event_present: bool
) -> None:
    refused = {"to_aid": 0, "summary": "s", "content": "c"}
    accepted = {"to_role": " Coder ", "summary": "s", "content": "c"}
    messages = _messages(refused, "Error: an agent cannot message itself.")
    messages += _messages(accepted, "Message queued to aid 1.", "accepted", request_id="accepted")
    events = [_decision(refused, 0, "", event_type="message_refused")]
    if success_event_present:
        events.append(_decision(accepted, 1, "accepted-message"))
    rows = _reported_cell(tmp_path, messages, events, tail)
    seat = rows[0].seats["0"]
    assert seat.msg_agent == 2 and seat.msg_agent_sent == 1
    assert seat.msg_agent_targets == ["role:coder"]
    assert rows[0].edges_walked == 1
    summary = cell_report.summarize(rows, None)
    assert summary["message_agent_attempts"] == 2 and summary["message_agent_sent"] == 1


@pytest.mark.parametrize("event_count,receipt_count", [(3, 3), (1, 3), (3, 1)])
def test_each_same_target_message_pairs_with_at_most_one_receipt(
    tmp_path: Path, event_count: int, receipt_count: int
) -> None:
    arguments = {"to_aid": 1, "summary": "same", "content": "same"}
    messages = []
    for index in range(receipt_count):
        messages += _messages(arguments, "Message queued to aid 1.", f"call{index}", request_id=f"call{index}")
    events = [_decision(arguments, 1, f"message-{index}") for index in range(event_count)]
    rows = _reported_cell(tmp_path, messages, events + events)
    seat = rows[0].seats["0"]
    assert seat.msg_agent == receipt_count
    assert seat.msg_agent_sent == 3
    assert rows[0].edges_walked == 1


def test_matching_uses_resolved_aid_and_payload_per_send(tmp_path: Path) -> None:
    earlier = {"to_aid": 1, "summary": "first", "content": "first"}
    later = {"to_role": " Coder ", "summary": "later", "content": "later"}
    tester = {"to_aid": 2, "summary": "test", "content": "test"}
    messages = _messages(earlier, "Message queued to aid 1.")
    messages += _messages(later, "Message queued to aid 1.", "later", request_id="later")
    messages += _messages(tester, "Message queued to aid 2.", "tester", request_id="tester")
    rows = _reported_cell(tmp_path, messages, [_decision(later, 1, "message-later")])
    seat = rows[0].seats["0"]
    assert seat.msg_agent == 3 and seat.msg_agent_sent == 3
    assert seat.msg_agent_targets == ["role:coder", "aid:1", "aid:2"]
    assert rows[0].edges_walked == 2


def test_detailed_decisions_pair_before_legacy_target_only_events(tmp_path: Path) -> None:
    first = {"to_aid": 1, "summary": "first", "content": "first"}
    later = {"to_aid": 1, "summary": "later", "content": "later"}
    messages = _messages(first, "Message queued to aid 1.")
    messages += _messages(later, "Message queued to aid 1.", "later", request_id="later")
    legacy = {"type": "message_sent", "payload": {"from_aid": 0, "to_aid": 1, "message_id": "legacy"}}
    rows = _reported_cell(tmp_path, messages, [legacy, _decision(first, 1, "current")])
    assert rows[0].seats["0"].msg_agent_sent == 2


def test_decisions_pair_with_receipts_from_the_same_sender(tmp_path: Path) -> None:
    arguments = {"to_aid": 2, "summary": "s", "content": "c"}
    event = _decision(arguments, 2, "other-sender-message")
    event["payload"]["from_aid"] = 1
    rows = _reported_cell(tmp_path, _messages(arguments, "Message queued to aid 2."), [event])
    assert rows[0].seats["0"].msg_agent_sent == 1
    assert rows[0].seats["1"].msg_agent_sent == 1


@pytest.mark.parametrize(
    "field,value",
    [("to_aid", 2), ("summary", "other"), ("summary_chars", 99), ("content_chars", 99), ("content_bytes", 99)],
)
def test_an_unmatched_success_event_keeps_the_successful_receipt(tmp_path: Path, field: str, value) -> None:
    arguments = {"to_aid": 1, "summary": "s", "content": "c"}
    event = _decision(arguments, 1, "other-message")
    event["payload"][field] = value
    rows = _reported_cell(tmp_path, _messages(arguments, "Message queued to aid 1."), [event])
    assert rows[0].seats["0"].msg_agent_sent == 2


@pytest.mark.parametrize("reason", ["self_message", "inbox_message_limit", "scheduler_shutting_down"])
def test_refusal_receipts_and_events_count_as_attempts(tmp_path: Path, reason: str) -> None:
    arguments = {"to_aid": 1, "summary": "s", "content": "c"}
    event = _decision(arguments, 1, "refused-message", event_type="message_refused")
    event["payload"]["reason"] = reason
    rows = _reported_cell(tmp_path, _messages(arguments, "Error: message refused."), [event])
    seat = rows[0].seats["0"]
    assert seat.msg_agent == 1 and seat.msg_agent_sent == 0
    assert rows[0].edges_walked == 0


def test_later_delivery_refusal_preserves_successfully_queued_count(tmp_path: Path) -> None:
    arguments = {"to_aid": 1, "summary": "s", "content": "c"}
    sent = _decision(arguments, 1, "message-1")
    refused = _decision(arguments, 1, "message-1", event_type="message_refused")
    refused["payload"]["restored"] = True
    rows = _reported_cell(tmp_path, _messages(arguments, "Message queued to aid 1."), [sent, refused])
    assert rows[0].seats["0"].msg_agent_sent == 1
    assert rows[0].edges_walked == 1


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("tail", ["", '{"type":"message_sent","payload":'])
def test_equal_size_commit_handoffs_keep_distinct_successes(tmp_path: Path, complete: bool, tail: str) -> None:
    first = {"to_aid": 1, "summary": "candidate", "content": "candidate " + "a" * 40}
    later = {**first, "content": "candidate " + "b" * 40}
    messages = _messages(first, "Message queued to aid 1.")
    messages += _messages(later, "Message queued to aid 1." if complete else None, "later", request_id="later")
    events = [_decision(later, 1, "message-later")]
    events[0]["payload"].update(commit_refs=["b" * 40], commit_refs_found=1)
    if complete:
        prior = _decision(first, 1, "message-first")
        prior["payload"].update(commit_refs=["a" * 40], commit_refs_found=1)
        events.insert(0, prior)
    rows = _reported_cell(tmp_path, messages, events + events, tail)
    assert rows[0].seats["0"].msg_agent_sent == 2
    assert cell_report.summarize(rows, None)["message_agent_sent"] == 2


@pytest.mark.parametrize("saved_in", ["pending_messages", "messages"])
@pytest.mark.parametrize("same_body", [False, True])
@pytest.mark.parametrize("complete", [False, True])
def test_recipient_message_ids_recover_exact_bodies_and_repeated_sends(
    tmp_path: Path, saved_in: str, same_body: bool, complete: bool
) -> None:
    first = {"to_aid": 1, "summary": "same", "content": "first & <text>\n"}
    later = {**first, "content": first["content"] if same_body else "later & <text>\n"}
    messages = _messages(first, "Message queued to aid 1.")
    messages += _messages(later, "Message queued to aid 1." if complete else None, "later", request_id="later")
    events = [_decision(later, 1, "message-later")]
    if complete:
        events.insert(0, _decision(first, 1, "message-first"))
    _reported_cell(tmp_path, messages, events + events)
    receiver = _runtime_dir(tmp_path / "cell", "team", "case-a") / "agent_1_coder.json"
    snapshot = json.loads(receiver.read_text())
    snapshot[saved_in] = [{
        "role": "user",
        "from_aid": 0,
        "to_aid": 1,
        "message_id": message_id,
        "message_content": arguments["content"],
        "content": (
            f'<teammate-message teammate_id="A0" summary={quoteattr(arguments["summary"])} '
            f'message_id={quoteattr(message_id)}>\n{escape(arguments["content"])}\n</teammate-message>'
        ),
    } for arguments, message_id in [(first, "message-first"), (later, "message-later")]]
    receiver.write_text(json.dumps(snapshot))
    rows = cell_report.run_rows(tmp_path / "cell", "team")
    expected = 1 if saved_in == "messages" and same_body and not complete else 2
    assert rows[0].seats["0"].msg_agent_sent == expected
    assert rows[0].edges_walked == 1


def test_tool_call_identity_pairs_before_a_content_compatible_event(tmp_path: Path) -> None:
    arguments = {"to_aid": 1, "summary": "same", "content": "same"}
    messages = _messages(arguments, "Message queued to aid 1.")
    matching = _decision(arguments, 1, "matching-message")
    matching["payload"]["tool_call_id"] = "call1"
    other = _decision(arguments, 1, "other-message")
    other["payload"]["tool_call_id"] = "other-call"
    rows = _reported_cell(tmp_path, messages, [other, matching])
    assert rows[0].seats["0"].msg_agent_sent == 2


def test_equal_dimensions_preserve_the_confirmed_send_floor(tmp_path: Path) -> None:
    first = {"to_aid": 1, "summary": "same", "content": "first"}
    later = {**first, "content": "later"}
    messages = _messages(first, "Message queued to aid 1.")
    messages += _messages(later, "Message queued to aid 1.", "later", request_id="later")
    rows = _reported_cell(tmp_path, messages, [_decision(later, 1, "message-later")])
    assert rows[0].seats["0"].msg_agent_sent == 2


def test_ambiguous_events_use_maximum_one_to_one_overlap(tmp_path: Path) -> None:
    first = {"to_aid": 1, "summary": "first", "content": "first"}
    later = {**first, "summary": "later", "content": "later"}
    messages = _messages(first, "Message queued to aid 1.")
    messages += _messages(later, "Message queued to aid 1.", "later", request_id="later")
    broad = _decision(first, 1, "broad")
    broad["payload"].pop("summary")
    precise = _decision(first, 1, "precise")
    precise["payload"].pop("summary_chars")
    rows = _reported_cell(tmp_path, messages, [broad, precise])
    assert rows[0].seats["0"].msg_agent_sent == 2


def test_commit_refs_compare_truncated_prefix_and_total(tmp_path: Path) -> None:
    arguments = {"to_aid": 1, "summary": "a" * 7, "content": "b" * 7 + " " + "a" * 7}
    event = _decision(arguments, 1, "message-1")
    event["payload"].update(commit_refs=["a" * 7], commit_refs_found=2)
    rows = _reported_cell(tmp_path, _messages(arguments, "Message queued to aid 1."), [event])
    assert rows[0].seats["0"].msg_agent_sent == 1
    event["payload"]["commit_refs_found"] = 1
    rows = _reported_cell(tmp_path / "mismatch", _messages(arguments, "Message queued to aid 1."), [event])
    assert rows[0].seats["0"].msg_agent_sent == 2


@pytest.mark.parametrize("saved_in,kind", [("messages", None), ("pending_messages", "stop_notice")])
def test_quoted_envelopes_and_administrative_notices_do_not_add_sends(tmp_path: Path, saved_in: str, kind) -> None:
    _reported_cell(tmp_path, [], [])
    receiver = _runtime_dir(tmp_path / "cell", "team", "case-a") / "agent_1_coder.json"
    snapshot = json.loads(receiver.read_text())
    snapshot[saved_in] = [{
        "role": "user", "from_aid": 0, "to_aid": 1, "message_id": "notice", "kind": kind,
        "message_content": "notice",
        "content": '<teammate-message teammate_id="A0" summary="s" message_id="notice">\nnotice\n</teammate-message>',
    }]
    receiver.write_text(json.dumps(snapshot))
    rows = cell_report.run_rows(tmp_path / "cell", "team")
    assert rows[0].seats["0"].msg_agent_sent == 0


@pytest.mark.parametrize("field,value", [("from_aid", 2), ("to_aid", 2), ("to_aid", True)])
def test_pending_envelope_identity_must_agree_with_structured_queue(tmp_path: Path, field: str, value) -> None:
    _reported_cell(tmp_path, [], [])
    receiver = _runtime_dir(tmp_path / "cell", "team", "case-a") / "agent_1_coder.json"
    snapshot = json.loads(receiver.read_text())
    pending = {
        "role": "user", "from_aid": 0, "to_aid": 1, "message_id": "message-1", "message_content": "body",
        "content": '<teammate-message teammate_id="A0" summary="s" message_id="message-1">\nbody\n</teammate-message>',
    }
    pending[field] = value
    snapshot["pending_messages"] = [pending]
    receiver.write_text(json.dumps(snapshot))
    rows = cell_report.run_rows(tmp_path / "cell", "team")
    assert rows[0].seats["0"].msg_agent_sent == 0


@pytest.mark.parametrize("snapshot", [None, "{", "null"])
def test_recipient_evidence_read_failure_keeps_existing_trace_facts(tmp_path: Path, snapshot) -> None:
    path = tmp_path / "agent_1_coder.json"
    if snapshot is not None:
        path.write_text(snapshot)
    event = _decision({"to_aid": 1, "summary": "s", "content": "c"}, 1, "message-1")
    (tmp_path / "trajectory.jsonl").write_text(json.dumps(event) + "\n")
    _, _, _, messages = _event_log_facts([path])
    assert messages["0"] == [event["payload"]]
