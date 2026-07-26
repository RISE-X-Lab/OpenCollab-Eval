"""Advisory OpenHands stop hook for an empty candidate workspace."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path

from opencollab_eval.patch_paths import (
    is_generated_dependency_artifact_path,
    is_generated_python_bytecode_path,
    is_generated_python_test_artifact_path,
)

from .gen_prediction_workflow import _patch_paths


def _state_path(env: Mapping[str, str]) -> Path:
    return Path(env.get("OPENHANDS_OUTPUT_DIR") or ".") / "empty_patch_stop_guard.json"


def _load_rejections(path: Path) -> int:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return int(value.get("rejections") or 0) if isinstance(value, dict) else 0


def _write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _container_patch(container_id: str, workspace: str) -> str:
    command = (
        f"cd {shlex.quote(workspace)} && "
        "git add -N -- . >/dev/null 2>&1 || true; git diff --binary HEAD --"
    )
    result = subprocess.run(
        ["docker", "exec", container_id, "sh", "-lc", command],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"container diff failed with exit {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout


def _candidate_paths(patch: str) -> tuple[list[str], list[str]]:
    paths = _patch_paths(patch)
    generated = [
        path
        for path in paths
        if is_generated_dependency_artifact_path(path)
        or is_generated_python_bytecode_path(path)
        or is_generated_python_test_artifact_path(path)
    ]
    return [path for path in paths if path not in generated], generated


def evaluate_stop(env: Mapping[str, str] | None = None) -> dict:
    values = os.environ if env is None else env
    container_id = str(values.get("OPENHANDS_CONTAINER_ID") or "").strip()
    workspace = str(values.get("OPENHANDS_WORKSPACE") or "/testbed").strip()
    max_rejections = max(0, int(values.get("OPENHANDS_EMPTY_PATCH_REJECTIONS") or 0))
    state_path = _state_path(values)
    if not container_id:
        return {"decision": "allow", "reason": "missing_container_id"}
    try:
        patch = _container_patch(container_id, workspace)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return {
            "decision": "allow",
            "reason": "patch_guard_error",
            "additionalContext": str(exc),
        }
    candidate_paths, generated_paths = _candidate_paths(patch)
    probe = {"status": "handled_by_workspace_integrity", "paths": []}
    if candidate_paths:
        _write_state(
            state_path,
            {
                "rejections": _load_rejections(state_path),
                "accepted": True,
                "source_paths": candidate_paths,
                "validation_paths": [],
                "generated_paths": generated_paths,
                "gitlink_probe": probe,
            },
        )
        return {"decision": "allow", "reason": "candidate_change_present"}

    rejections = _load_rejections(state_path)
    exhausted = rejections >= max_rejections
    if not exhausted:
        rejections += 1
    _write_state(
        state_path,
        {
            "rejections": rejections,
            "accepted": False,
            "exhausted": exhausted,
            "validation_paths": [],
            "generated_paths": generated_paths,
            "gitlink_probe": probe,
        },
    )
    if exhausted:
        return {"decision": "allow", "reason": "empty_patch_rejection_limit_reached"}
    detail = (
        "Only disposable generated artifacts changed: " + ", ".join(generated_paths)
        if generated_paths
        else "No candidate repository change was found."
    )
    return {
        "decision": "deny",
        "reason": "empty_candidate",
        "additionalContext": (
            f"{detail} Continue this task and make a concrete repository change. "
            f"Empty-patch rejection {rejections}/{max_rejections}."
        ),
    }


def main() -> int:
    print(json.dumps(evaluate_stop(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
