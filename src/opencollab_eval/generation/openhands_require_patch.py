"""OpenHands stop hook that rejects completion without a source patch."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path

from opencollab_eval.patch_gitlinks import (
    gitlink_deletion_candidates,
    parse_ls_tree_entries,
)
from opencollab_eval.patch_paths import (
    is_generated_dependency_artifact_path,
    is_generated_python_bytecode_path,
    is_generated_python_test_artifact_path,
)

from .gen_prediction_workflow import (
    _looks_like_validation_artifact,
    _patch_paths,
)


def _state_path(env: Mapping[str, str]) -> Path:
    output_dir = Path(env.get("OPENHANDS_OUTPUT_DIR") or ".")
    return output_dir / "empty_patch_stop_guard.json"


def _load_rejections(path: Path) -> int:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return int(value.get("rejections") or 0) if isinstance(value, dict) else 0


def _write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _container_patch(container_id: str, workspace: str) -> str:
    command = f"cd {shlex.quote(workspace)} && git diff --binary HEAD --"
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


def _container_gitlink_probe(
    container_id: str,
    workspace: str,
    patch: str,
    expected_removed: dict[str, str],
) -> dict:
    all_candidates = gitlink_deletion_candidates(patch)
    candidates = [
        item
        for item in all_candidates
        if expected_removed.get(str(item["path"])) == item["old_oid"]
    ]
    evidence = {
        "status": "no_candidates",
        "source_patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        "paths": [
            {
                "path": item["path"],
                "old_oid": item["old_oid"],
                "base_oid": "",
                "probe_status": "not_snapshot_removed",
            }
            for item in all_candidates
            if item not in candidates
        ],
    }
    if not candidates:
        if all_candidates:
            evidence["status"] = "no_eligible_candidates"
        return evidence
    command = [
        "docker",
        "exec",
        container_id,
        "env",
        "-u",
        "GIT_DIR",
        "-u",
        "GIT_WORK_TREE",
        "-u",
        "GIT_INDEX_FILE",
        "-u",
        "GIT_OBJECT_DIRECTORY",
        "-u",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "-u",
        "GIT_COMMON_DIR",
        "-u",
        "GIT_CEILING_DIRECTORIES",
        "-u",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_CONFIG_NOSYSTEM=1",
        "GIT_CONFIG_GLOBAL=/dev/null",
        "GIT_NO_REPLACE_OBJECTS=1",
        "git",
        "--literal-pathspecs",
        "-C",
        workspace,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "ls-tree",
        "-z",
        "HEAD",
        "--",
        *(str(item["path"]) for item in candidates),
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        entries = parse_ls_tree_entries(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        evidence["status"] = "probe_failed"
        return evidence
    for item in candidates:
        base = entries.get(str(item["path"]), {})
        status = (
            "verified"
            if base.get("base_mode") == "160000"
            and base.get("base_type") == "commit"
            and base.get("base_oid") == item["old_oid"]
            else "mismatch"
        )
        evidence["paths"].append(
            {
                "path": item["path"],
                "old_oid": item["old_oid"],
                "base_oid": str(base.get("base_oid") or ""),
                "probe_status": status,
            }
        )
    eligible_paths = {str(item["path"]) for item in candidates}
    eligible_evidence = [
        item for item in evidence["paths"] if str(item["path"]) in eligible_paths
    ]
    evidence["status"] = (
        "verified"
        if len(eligible_evidence) == len(candidates)
        and all(item["probe_status"] == "verified" for item in eligible_evidence)
        else "baseline_mismatch"
    )
    return evidence


def _expected_removed_gitlinks(values: Mapping[str, str]) -> dict[str, str]:
    raw = str(values.get("OPENHANDS_REMOVED_GITLINKS_JSON") or "[]")
    if len(raw.encode("utf-8")) > 256 * 1024:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, list) or len(parsed) > 1024:
        return {}
    result: dict[str, str] = {}
    for item in parsed:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "old_oid"}
            or not isinstance(item.get("path"), str)
            or not item["path"]
            or "\x00" in item["path"]
            or not isinstance(item.get("old_oid"), str)
            or len(item["old_oid"]) not in {40, 64}
            or any(char not in "0123456789abcdef" for char in item["old_oid"])
            or item["path"] in result
        ):
            return {}
        result[item["path"]] = item["old_oid"]
    return result


def _source_paths(
    patch: str,
    *,
    verified_gitlinks: set[str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    paths = _patch_paths(patch)
    verified = verified_gitlinks or set()
    validation = [path for path in paths if _looks_like_validation_artifact(path)]
    generated = [
        path
        for path in paths
        if is_generated_dependency_artifact_path(path)
        or is_generated_python_bytecode_path(path)
        or is_generated_python_test_artifact_path(path)
        or path in verified
    ]
    source = [path for path in paths if path not in validation and path not in generated]
    return source, validation, generated


def evaluate_stop(env: Mapping[str, str] | None = None) -> dict:
    values = os.environ if env is None else env
    container_id = str(values.get("OPENHANDS_CONTAINER_ID") or "").strip()
    workspace = str(values.get("OPENHANDS_WORKSPACE") or "/testbed").strip()
    max_rejections = max(
        0, int(values.get("OPENHANDS_EMPTY_PATCH_REJECTIONS") or 0)
    )
    state_path = _state_path(values)
    if not container_id:
        return {
            "decision": "allow",
            "reason": "missing_container_id",
        }
    try:
        patch = _container_patch(container_id, workspace)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return {
            "decision": "allow",
            "reason": "patch_guard_error",
            "additionalContext": str(exc),
        }
    gitlink_probe = _container_gitlink_probe(
        container_id,
        workspace,
        patch,
        _expected_removed_gitlinks(values),
    )
    verified_gitlinks = {
        str(item["path"])
        for item in gitlink_probe["paths"]
        if item.get("probe_status") == "verified"
    }
    source_paths, validation_paths, generated_paths = _source_paths(
        patch,
        verified_gitlinks=verified_gitlinks,
    )
    if source_paths:
        _write_state(
            state_path,
            {
                "rejections": _load_rejections(state_path),
                "accepted": True,
                "source_paths": source_paths,
                "validation_paths": validation_paths,
                "generated_paths": generated_paths,
                "gitlink_probe": gitlink_probe,
            },
        )
        return {
            "decision": "allow",
            "reason": "source_patch_present",
        }

    rejections = _load_rejections(state_path)
    if rejections >= max_rejections:
        _write_state(
            state_path,
            {
                "rejections": rejections,
                "accepted": False,
                "exhausted": True,
                "validation_paths": validation_paths,
                "generated_paths": generated_paths,
                "gitlink_probe": gitlink_probe,
            },
        )
        return {
            "decision": "allow",
            "reason": "empty_patch_rejection_limit_reached",
        }

    rejections += 1
    _write_state(
        state_path,
        {
            "rejections": rejections,
            "accepted": False,
            "exhausted": False,
            "validation_paths": validation_paths,
            "generated_paths": generated_paths,
            "gitlink_probe": gitlink_probe,
        },
    )
    if validation_paths:
        detail = "Only validation/test files changed: " + ", ".join(validation_paths)
    elif generated_paths:
        detail = "Only generated files changed: " + ", ".join(generated_paths)
    else:
        detail = "No tracked source file has changed."
    return {
        "decision": "deny",
        "reason": "empty_source_patch",
        "additionalContext": (
            f"{detail} Continue this same task. Re-check the issue and relevant tests, "
            "then make a concrete minimal change to the implementation inside the "
            "existing Docker container. Run git diff --stat in the container and do "
            "not call finish again until at least one non-test tracked source file changed. "
            f"Empty-patch rejection {rejections}/{max_rejections}."
        ),
    }


def main() -> int:
    print(json.dumps(evaluate_stop(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
