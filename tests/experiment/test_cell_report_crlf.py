"""Message reports preserve line endings from real scheduler queue envelopes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from opencollab.adapters.tools.message import MessageAgentTool
from opencollab.adapters.trace import Tracer
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.application.session import Session
from opencollab.application.tool_execution import ToolRuntime
from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionState

from opencollab_eval.experiment.cell_report_messaging import received_events
from opencollab_eval.experiment.cell_report_rows import run_rows


async def _sink(event):
    pass


class LocalSession:
    def __init__(self, role):
        self.state = SessionState(messages=[])
        self.agent = SimpleNamespace(name=role)
        self.used_tokens = 0
        self._turn_lock = asyncio.Lock()
        self._loop_checkpoint_step = 0
        self._loop_checkpoint_results = []
        self._loop_checkpoint_prefix = []
        self.runner = SimpleNamespace(pending_cleanup_tasks=(), reset_runtime_for_user_turn=lambda: None)
        self.event_bus = EventBus(_sink)

    async def add_user_message(self, content):
        await Session.add_user_message(self, content)

    async def run_loop(self):
        self.state.consume_queued_external_user_turn()
        self.state.append_message({"role": "assistant", "content": "received"})
        self.state.mark_done()
        return "received"

    def snapshot(self):
        messages, meta = Session._snapshot_for_save(self)
        return dict(meta, messages=messages)


@pytest.mark.xfail(strict=True, reason="P2-08 XML parsing normalizes saved message line endings")
@pytest.mark.parametrize("ending", ["\r\n", "\r"])
@pytest.mark.parametrize("state", ["queued", "delivered", "recovered"])
async def test_scheduler_line_endings_preserve_send_count(tmp_path: Path, ending: str, state: str):
    runtime = tmp_path / "logs-team/case-a/trajectories/task-local/runtime-local"
    runtime.mkdir(parents=True)
    tracer = Tracer(run_id="line-endings", output_dir=str(runtime), filename="trajectory.jsonl")
    scheduler = Scheduler(
        session_factory=SimpleNamespace(),
        worktree_pool=WorktreePool(str(tmp_path), use_worktrees=False),
        event_sink=EventBus(_sink), tracer=tracer, roles=("lead", "coder"),
    )
    lead, child = LocalSession("lead"), LocalSession("coder")
    scheduler.register_lead(lead)
    child.state.aid = 1
    scheduler.table.add(SessionControlBlock(aid=1, parent_aid=0, agent=child.agent, state=child.state))
    scheduler._sessions[1] = child
    blocker = asyncio.get_running_loop().create_future()
    scheduler._tasks[1] = blocker
    bodies = [f"line1{ending}first", f"line1{ending}other"]
    try:
        for index, body in enumerate(bodies):
            arguments = {"to_role": "coder", "summary": "handoff", "content": body}
            call_id = f"call-{index}"
            lead.state.append_message({"role": "assistant", "tool_calls": [{
                "id": call_id, "function": {"name": "message_agent", "arguments": json.dumps(arguments)},
            }]})
            receipt = await MessageAgentTool(scheduler).execute_with_runtime(
                arguments, ToolRuntime(environment=None, safety_policy=None, permission_policy=None,
                                       aid=0, tool_call_id=call_id),
            )
            lead.state.append_message({"role": "tool", "tool_call_id": call_id, "content": receipt})
        if state == "delivered":
            blocker.cancel()
            await scheduler._drain_message_inbox(1)
            await scheduler.wait_until_terminal(1)
        tracer.flush()
        if state == "recovered":
            lead.state.messages.clear()
            lead.state.message_timestamps.clear()
            (runtime / "trajectory.jsonl").write_text('{"type":"message_sent","payload":')
        for aid, session, role in [(0, lead, "lead"), (1, child, "coder")]:
            (runtime / f"agent_{aid}_{role}.json").write_text(json.dumps(session.snapshot()))
        (tmp_path / "metrics.jsonl").write_text(json.dumps({
            "instance_id": "case-a", "trajectory_path": str(runtime / "trajectory.jsonl"),
            "run_summary": {"status": "completed"},
        }) + "\n")
        rows = run_rows(tmp_path, "team")
        assert rows[0].seats["0"].msg_agent_sent == 2
        assert [event["content"] for event, _queued in received_events(child.snapshot())] == bodies
    finally:
        blocker.cancel()
        tracer.close()
