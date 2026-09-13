"""Large completed trajectories retain identity checks across the entire file."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager

import pytest

from opencollab_eval.generation import trajectory_identity


def _call(path):
    return trajectory_identity.verified_provider_models(
        str(path),
        artifact_root=path.parent,
        expected_model="test-model",
        expected_reasoning_effort="max",
        wire_protocol="responses",
    )


def _record(model="test-model"):
    return (
        json.dumps(
            {
                "type": "llm_call",
                "payload": {
                    "wire_protocol": "responses",
                    "provider_model": model,
                    "reasoning_effort": "max",
                    "reasoning_effort_policy": "configured",
                },
            }
        )
        + "\n"
    ).encode()


def test_large_trajectory_retains_full_digest_and_checks_identity_after_old_limit(tmp_path):
    path = tmp_path / "trajectory.jsonl"
    padding = (json.dumps({"type": "tool_exec", "payload": {"result": "x" * (1024 * 1024)}}) + "\n").encode()
    digest = hashlib.sha256()
    with path.open("wb") as output:
        for chunk in [_record(), *([padding] * 17), _record()]:
            output.write(chunk)
            digest.update(chunk)
    assert path.stat().st_size > 16 * 1024 * 1024
    assert _call(path) == (["test-model"], digest.hexdigest())
    with path.open("ab") as output:
        output.write(_record("wrong-model"))
    with pytest.raises(RuntimeError, match="provider model mismatch"):
        _call(path)


def test_single_oversized_record_is_still_rejected(tmp_path):
    path = tmp_path / "trajectory.jsonl"
    path.write_bytes(_record() + b'{"type":"tool_exec","payload":"' + b"x" * (16 * 1024 * 1024) + b'"}\n')
    with pytest.raises(RuntimeError, match="record exceeds 16 MiB"):
        _call(path)


def test_concurrent_append_remains_detected(tmp_path, monkeypatch):
    path = tmp_path / "trajectory.jsonl"
    path.write_bytes(_record())
    original = trajectory_identity.open_regular_binary

    class AppendingReader:
        def __init__(self, handle):
            self.handle = handle

        def fileno(self):
            return self.handle.fileno()

        def readline(self, size):
            result = self.handle.readline(size)
            with path.open("ab") as output:
                output.write(_record())
            return result

    @contextmanager
    def opening(path):
        with original(path) as handle:
            yield AppendingReader(handle)

    monkeypatch.setattr(trajectory_identity, "open_regular_binary", opening)
    with pytest.raises(RuntimeError, match="changed while reading"):
        _call(path)
