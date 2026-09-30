"""A new score invocation executes again while an explicit run ID resumes its cache."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab_eval.commands.batch_scoring import cmd_score
from opencollab_eval.experiment import batch_score
from tests.experiment.batch_support import ScoreRemote, _score_guard


def _batch(tmp_path: Path) -> SimpleNamespace:
    frame = tmp_path / "frame.jsonl"
    frame.write_text(json.dumps({"instance_id": "case-a", "patch": "gold patch"}) + "\n")
    return SimpleNamespace(
        spec=SimpleNamespace(name="cell", arm="single"),
        host=SimpleNamespace(
            workdir="/remote", ssh="host", scoring_dataset="/dataset.jsonl", scoring_python="/python",
            docker_disk="/disk", min_free_gb=1,
        ),
        rows=[{"instance_id": "case-a"}],
        frame_content=frame,
        local_dir=tmp_path,
        spec_path=tmp_path / "spec.yaml",
    )


def _launch_ids(remote: ScoreRemote) -> list[str]:
    ids = []
    for script in remote.scripts:
        if "--run_id" in script:
            argv = shlex.split(script)
            ids.append(argv[argv.index("--run_id") + 1])
    return ids


@pytest.mark.parametrize("gold", [False, True])
def test_new_invocations_use_distinct_harness_cache_names(tmp_path: Path, gold: bool) -> None:
    batch = _batch(tmp_path)
    remote = ScoreRemote(_score_guard(rows="1"), dataset="DATASET_ROWS\t1\n")
    for _ in range(2):
        assert cmd_score(batch, remote, max_workers=1, timeout=30, gold=gold) == 0
    first, second = _launch_ids(remote)
    assert first != second
    assert batch_score.run_stamp(first, batch.spec, gold=gold)
    assert batch_score.run_stamp(second, batch.spec, gold=gold)


@pytest.mark.parametrize("gold", [False, True])
def test_explicit_run_id_keeps_the_existing_cache_namespace(tmp_path: Path, gold: bool) -> None:
    batch = _batch(tmp_path)
    remote = ScoreRemote(_score_guard(rows="1"), dataset="DATASET_ROWS\t1\n")
    old_id = f"cell{'-gold' if gold else ''}-20260930"
    for _ in range(2):
        assert cmd_score(batch, remote, max_workers=1, timeout=30, gold=gold, run_id=old_id) == 0
    assert _launch_ids(remote) == [old_id, old_id]


def test_resume_id_matches_the_selected_batch_and_scoring_mode(tmp_path: Path) -> None:
    remote = ScoreRemote(_score_guard(rows="1"))
    assert cmd_score(_batch(tmp_path), remote, max_workers=1, timeout=30, gold=True, run_id="cell-20260930") == 1
    assert remote.scripts == []
