from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencollab_eval.generation import openhands_require_patch as guard


def env(tmp_path: Path, *, rejections: int = 2) -> dict[str, str]:
    return {
        "OPENHANDS_CONTAINER_ID": "container",
        "OPENHANDS_WORKSPACE": "/testbed",
        "OPENHANDS_OUTPUT_DIR": str(tmp_path),
        "OPENHANDS_EMPTY_PATCH_REJECTIONS": str(rejections),
    }


@pytest.mark.parametrize(
    "path",
    (
        "app/main.py",
        "tests/test_app.py",
        "fixtures/public.json",
        "vendor/module",
    ),
)
def test_stop_guard_accepts_every_concrete_candidate_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    monkeypatch.setattr(
        guard,
        "_container_patch",
        lambda *_args: f"diff --git a/{path} b/{path}\n+changed\n",
    )

    result = guard.evaluate_stop(env(tmp_path))

    assert result == {"decision": "allow", "reason": "candidate_change_present"}
    state = json.loads((tmp_path / "empty_patch_stop_guard.json").read_text(encoding="utf-8"))
    assert state["source_paths"] == [path]
    assert state["gitlink_probe"]["status"] == "handled_by_workspace_integrity"


def test_stop_guard_rejects_only_disposable_generated_artifacts_then_exhausts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch = (
        "diff --git a/.yarn/install-state.gz b/.yarn/install-state.gz\n"
        "new file mode 100644\n"
        "Binary files /dev/null and b/.yarn/install-state.gz differ\n"
    )
    monkeypatch.setattr(guard, "_container_patch", lambda *_args: patch)
    values = env(tmp_path, rejections=2)

    first = guard.evaluate_stop(values)
    second = guard.evaluate_stop(values)
    third = guard.evaluate_stop(values)

    assert first["decision"] == second["decision"] == "deny"
    assert first["reason"] == second["reason"] == "empty_candidate"
    assert third == {"decision": "allow", "reason": "empty_patch_rejection_limit_reached"}


def test_stop_guard_keeps_source_names_that_only_resemble_generated_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch = (
        "diff --git a/src/cache.pyc.py b/src/cache.pyc.py\n"
        "--- a/src/cache.pyc.py\n+++ b/src/cache.pyc.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
    monkeypatch.setattr(guard, "_container_patch", lambda *_args: patch)
    assert guard.evaluate_stop(env(tmp_path)) == {
        "decision": "allow",
        "reason": "candidate_change_present",
    }


def test_stop_guard_is_advisory_when_the_probe_cannot_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(guard, "_container_patch", fail)
    result = guard.evaluate_stop(env(tmp_path))
    assert result["decision"] == "allow"
    assert result["reason"] == "patch_guard_error"


def test_container_probe_adds_intent_to_add_for_untracked_candidate_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def fake_run(command, **_kwargs):
        seen.append(command)
        return guard.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    assert guard._container_patch("cid", "/testbed") == ""
    assert "git add -N -- ." in seen[0][-1]
