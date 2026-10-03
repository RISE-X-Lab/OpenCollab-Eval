"""The monitor discovers public single, workflow, and team snapshot layouts."""

from pathlib import Path

import pytest
from opencollab.adapters.storage import SessionStore

from opencollab_eval.commands import swebench_loop_monitor as monitor
from tests.orchestration.test_swebench_loop_journal import TEXT, _args


@pytest.mark.parametrize("filename", ["agent.json", "000_analyst.json", "001_coder-r1.json"])
@pytest.mark.parametrize("journal_only", [False, True])
def test_public_snapshot_layouts_are_discovered(tmp_path: Path, filename: str, journal_only: bool):
    path = tmp_path / "artifacts" / filename
    path.parent.mkdir()
    messages = [{"role": "assistant", "content": TEXT} for _ in range(4)]
    store = SessionStore()
    if journal_only:
        store.append_snapshot_delta(str(path), sequence=1, replace_from=0, messages=messages,
                                    meta={"aid": -1, "role": "swe_agent"})
    else:
        store.save(str(path), messages, meta={"aid": -1, "role": "swe_agent"})
    report = monitor.build_report(_args(tmp_path))
    assert report["input_complete"] is True
    assert report["session_files_discovered"] == 1
    assert report["session_files"] == [str(path)]
    assert report["max_repeated_sentence_count"] == 4
    assert report["level"] == "critical"


def test_team_snapshot_and_journal_are_discovered_once(tmp_path: Path):
    path = tmp_path / "agent_1_coder.json"
    store = SessionStore()
    store.save(str(path), [{"role": "user", "content": "task"}], meta={"aid": 1, "role": "coder"})
    store.append_snapshot_delta(str(path), sequence=1, replace_from=1,
                                messages=[{"role": "assistant", "content": TEXT}],
                                meta={"aid": 1, "role": "coder"})
    (tmp_path / "team.json").write_text('{}')
    (tmp_path / "metrics.json").write_text('{}')
    assert monitor._session_paths(tmp_path) == [path]
    messages, files, errors, discovered = monitor._session_messages_status(tmp_path)
    assert len(messages) == 2
    assert files == [str(path)]
    assert errors == []
    assert discovered == 1
