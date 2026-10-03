"""Public team messages preserve exact line endings in persisted send evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from openai.resources.chat.completions import AsyncCompletions
from openai.types.chat import ChatCompletion
from opencollab import OpenCollab

from opencollab_eval.experiment.cell_report_messaging import received_events
from opencollab_eval.experiment.cell_report_rows import run_rows


@pytest.mark.parametrize("ending", ["\r\n", "\r"])
@pytest.mark.parametrize("state", ["queued", "delivered", "recovered"])
async def test_scheduler_line_endings_preserve_send_count(tmp_path: Path, monkeypatch, ending: str, state: str):
    runtime = tmp_path / "logs-team/case-a/trajectories/task-local/runtime-local"
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\nroles:\n  lead:\n    tools: [message_agent]\n    prompt: LINE_ENDING_LEAD\n"
        "  coder:\n    tools: [file_read]\n    prompt: LINE_ENDING_CODER\n"
        "topology:\n  lead: [coder]\n  coder: []\n"
    )
    bodies = [f"line1{ending}first", f"line1{ending}other"]
    requests = []

    async def create_completion(resource, **payload):
        requests.append({"keys": list(payload), "stream": payload.get("stream")})
        lead = any("LINE_ENDING_LEAD" in str(msg.get("content")) for msg in payload["messages"])
        first = lead and not any(msg.get("role") == "tool" for msg in payload["messages"])
        message = {"role": "assistant", "content": "received"}
        if first:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call-{index}", "type": "function", "function": {
                    "name": "message_agent", "arguments": json.dumps({
                        "to_role": "coder", "summary": "handoff", "content": body,
                    }),
                },
            } for index, body in enumerate(bodies)]}
        return ChatCompletion(**{
            "id": "chatcmpl-offline", "object": "chat.completion", "created": 1, "model": "offline-model",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if first else "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        })

    monkeypatch.setattr(AsyncCompletions, "create", create_completion)
    client = OpenCollab(
        tmp_path, provider="openai", model="offline-model", api_key="offline-fixture",
        base_url="https://fixture.invalid/v1", config={
        "wire_protocol": "chat_completions", "llm_stream_chat": False, "llm_max_retries": 0,
        },
    )
    result = await client.team("Send both handoffs", config=team, artifacts=runtime, use_worktrees=False,
                               prebuild_team=True, serialize_turns=True, budget=100000, max_steps=4, timeout=5)
    assert result.status == "completed", f"{result}; requests={requests}"
    assert requests, "The public completion request must enter the offline SDK fixture"
    lead = next(runtime.glob("agent_0_*.json"))
    receiver = next(runtime.glob("agent_1_*.json"))
    snapshot = OpenCollab.read_session_snapshot(receiver)
    events = received_events(snapshot)
    assert [event["content"] for event, _queued in events] == bodies
    if state != "delivered":
        # Preserve the producer's envelopes in the supported pending-queue shape.
        envelopes = [msg for msg in snapshot["messages"] if msg.get("role") == "user"
                     and "<teammate-message" in str(msg.get("content"))]
        envelope_xml = [xml for msg in envelopes for xml in re.findall(
            r"<teammate-message\b[^>]*>[\s\S]*?</teammate-message>", msg["content"],
        )]
        assert len(envelope_xml) == 2
        snapshot["messages"] = [{"role": "user", "content": "task"}]
        snapshot["pending_messages"] = [{
            **event, "role": "user", "message_content": event["content"],
            "content": next(xml for xml in envelope_xml if event["message_id"] in xml),
        } for event, _queued in events]
        receiver.write_text(json.dumps(snapshot))
        Path(f"{receiver}.journal").unlink(missing_ok=True)
        assert OpenCollab.read_session_snapshot(receiver)["pending_messages"] == snapshot["pending_messages"]
    if state == "recovered":
        lead_snapshot = OpenCollab.read_session_snapshot(lead)
        lead_snapshot["messages"] = [{"role": "user", "content": "task"}]
        lead.write_text(json.dumps(lead_snapshot))
        Path(f"{lead}.journal").unlink(missing_ok=True)
        (runtime / "trajectory.jsonl").write_text('{"type":"message_sent","payload":')
    (tmp_path / "metrics.jsonl").write_text(json.dumps({
        "instance_id": "case-a", "trajectory_path": str(runtime / "trajectory.jsonl"),
        "run_summary": {"status": "completed"},
    }) + "\n")
    rows = run_rows(tmp_path, "team")
    assert rows[0].seats["0"].msg_agent_sent == 2
