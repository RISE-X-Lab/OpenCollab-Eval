"""Loop reports replay native journals and retain bounded ordinary-file reads."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from opencollab import OpenCollab

from opencollab_eval.commands import swebench_loop_monitor as monitor

TEXT = "I am inspecting the same test output once again before making the next source edit."


def _args(root):
    return SimpleNamespace(session_root=str(root), events_file=[], diff_file=None,
                           instance_id="case-a", output=str(root.parent / "report.json"))


def _write_snapshot(path, messages, meta):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**meta, "messages": messages}))


def _write_journal(path, *, replace_from, messages, meta):
    path.parent.mkdir(parents=True, exist_ok=True)
    Path(f"{path}.journal").write_text(json.dumps({
        "journal_version": 1, "sequence": 1, "replace_from": replace_from,
        "message_count": replace_from + len(messages), "messages": messages, "meta": meta,
        "seen_result_hashes_reset": False, "seen_result_hashes_added": [],
    }) + "\n")


def _native_journal(root: Path):
    path = root / "agent_0_analyst.json"
    first = [{"role": "user", "content": "fix the failing test"}]
    latest = [{"role": "assistant", "content": TEXT} for _ in range(4)] + [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "write-1", "type": "function", "function": {
                "name": "file_write", "arguments": '{"path":"a.py","content":"fixed"}',
            },
        }]},
        {"role": "tool", "tool_call_id": "write-1", "content": "Wrote 5 bytes to a.py"},
    ]
    _write_snapshot(path, first, {"aid": 0, "role": "analyst"})
    _write_journal(path, replace_from=1, messages=latest, meta={"aid": 0, "role": "analyst"})
    return path, first + latest


def test_loop_monitor_uses_latest_native_journal_state(tmp_path):
    path, messages = _native_journal(tmp_path)
    assert len(json.loads(path.read_text())["messages"]) == 1
    assert OpenCollab.read_session_snapshot(path)["messages"] == messages
    report = monitor.build_report(_args(tmp_path))
    assert report["input_complete"] is True
    assert report["level"] == "critical"
    assert report["max_repeated_sentence_count"] == 4
    assert report["last_successful_write"]["tool"] == "file_write"


@pytest.mark.parametrize("kind", ["malformed", "symlink", "oversized", "fifo"])
def test_loop_monitor_journal_failure_is_visible(tmp_path, monkeypatch, kind):
    path, _messages = _native_journal(tmp_path)
    journal = Path(str(path) + ".journal")
    if kind == "malformed":
        journal.write_text("invalid\n")
    elif kind == "symlink":
        journal.unlink()
        journal.symlink_to(path)
    elif kind == "fifo":
        import os
        journal.unlink()
        os.mkfifo(journal)
    else:
        monkeypatch.setattr(monitor, "MAX_SESSION_JSON_BYTES", path.stat().st_size + 1)
    report = monitor.build_report(_args(tmp_path))
    assert report["input_complete"] is False
    assert report["input_errors"]
