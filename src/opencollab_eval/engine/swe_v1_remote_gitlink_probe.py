"""Trusted official-image probes for generated Gitlink deletion artifacts."""

# ruff: noqa: F403, F405

from __future__ import annotations

import hashlib
import json
import pathlib

from opencollab_eval.engine.swe_generation_proof import (
    current_generation_proof_valid,
    solver_git_snapshot_valid,
)
from opencollab_eval.engine.swe_v1_remote_records import *
from opencollab_eval.patch_gitlinks import *

GITLINK_PROBE_SCHEMA = "opencollab.prolite_gitlink_probe.v1"
LEGACY_GITLINK_AUDIT_SCHEMA = "opencollab.prolite_gitlink_legacy_audit.v1"
MAX_GITLINK_PROBE_PATHS = 1024
MAX_GITLINK_PROBE_PATH_BYTES = 128 * 1024
_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PROBE_SCRIPT = r"""
set -eu
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
  GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_CEILING_DIRECTORIES \
  GIT_DISCOVERY_ACROSS_FILESYSTEM
export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_NO_REPLACE_OBJECTS=1
for repo in /app /testbed /workspace /repo /src; do
  if git --literal-pathspecs -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
    exec git --literal-pathspecs -C "$repo" \
      -c core.fsmonitor=false -c core.hooksPath=/dev/null \
      ls-tree -z "$@"
  fi
done
exit 125
"""


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _removed_gitlinks(value):
    if (
        not isinstance(value, list)
        or len(value) > MAX_GITLINK_PROBE_PATHS
        or any(
            not isinstance(item, dict)
            or set(item) != {"path", "old_oid"}
            or not isinstance(item.get("path"), str)
            or not item["path"]
            or "\x00" in item["path"]
            or not isinstance(item.get("old_oid"), str)
            or _OBJECT_ID_RE.fullmatch(item["old_oid"]) is None
            for item in value
        )
        or len({item["path"] for item in value}) != len(value)
        or sum(len(item["path"].encode("utf-8", errors="surrogatepass")) for item in value)
        > MAX_GITLINK_PROBE_PATH_BYTES
    ):
        return None
    return {(item["path"], item["old_oid"]) for item in value}


def _legacy_audited_removed_gitlinks(row, prediction, metric, source_patch_sha256):
    audit = metric.get("audited_legacy_gitlink_evidence") if isinstance(metric, dict) else None
    task = str(row.get("instance_id") or "")
    base_commit = str(row.get("base_commit") or row.get("commit") or "").strip().lower()
    if (
        not isinstance(audit, dict)
        or set(audit)
        != {
            "schema",
            "audit_id",
            "task",
            "base_commit",
            "source_patch_sha256",
            "removed_gitlinks",
        }
        or audit.get("schema") != LEGACY_GITLINK_AUDIT_SCHEMA
        or not isinstance(audit.get("audit_id"), str)
        or not audit["audit_id"].strip()
        or len(audit["audit_id"].encode("utf-8", errors="surrogatepass")) > 256
        or audit.get("task") != task
        or audit.get("base_commit") != base_commit
        or audit.get("source_patch_sha256") != source_patch_sha256
        or row_patch_sha(prediction) != source_patch_sha256
    ):
        return None
    return _removed_gitlinks(audit.get("removed_gitlinks"))


def _trusted_removed_gitlinks(row, prediction, metric, source_patch, source_patch_sha256):
    if current_generation_proof_valid(metric, source_patch):
        return set()
    return _legacy_audited_removed_gitlinks(row, prediction, metric, source_patch_sha256)


def _candidate_expectation(row, prediction, metric, selection):
    source_patch = prediction_patch(prediction)
    metric_row = metric if isinstance(metric, dict) else {}
    extraction = metric_row.get("trusted_patch_extraction")
    source_candidate_tree = ""
    source_base_commit = ""
    source_anonymous_base = ""
    source_base_tree = ""
    if current_generation_proof_valid(metric, source_patch) and isinstance(extraction, dict):
        snapshot = metric_row.get("solver_git_snapshot")
        source_candidate_tree = str(extraction.get("candidate_tree") or "")
        if isinstance(snapshot, dict):
            source_base_commit = str(snapshot.get("expected_base_commit") or "")
        source_anonymous_base = str(extraction.get("fixed_anonymous_base") or "")
        source_base_tree = str(extraction.get("base_tree") or "")
    identity = {
        "instance_id": str(row.get("instance_id") or ""),
        "record_id": row_record_id(prediction),
        "invocation_id": str(metric_row.get("invocation_id") or ""),
        "run_id": str(metric_row.get("run_id") or ""),
        "runtime_tree_sha256": str(metric_row.get("runtime_tree_sha256") or ""),
        "generation_image_id": str(metric_row.get("generation_image_id") or ""),
        "workflow": str(
            metric_row.get("workflow")
            or prediction.get("workflow")
            or prediction.get("workflow_name")
            or ""
        ),
        "model_name": str(
            metric_row.get("model_name")
            or metric_row.get("model_name_or_path")
            or prediction.get("model_name")
            or prediction.get("model_name_or_path")
            or ""
        ),
    }
    run_identity_sha256 = hashlib.sha256(_canonical_json(identity).encode()).hexdigest()
    expected_tree = (
        source_candidate_tree
        if selection["source_patch_sha256"] == selection["eval_patch_sha256"]
        else ""
    )
    return {
        "schema": "opencollab.eval_candidate_expectation.v1",
        "instance_id": identity["instance_id"],
        "record_id": identity["record_id"],
        "run_identity_sha256": run_identity_sha256,
        "source_patch_sha256": selection["source_patch_sha256"],
        "eval_patch_sha256": selection["eval_patch_sha256"],
        "source_base_commit": source_base_commit,
        "source_anonymous_base": source_anonymous_base,
        "source_base_tree": source_base_tree,
        "source_candidate_tree": source_candidate_tree,
        "expected_candidate_tree": expected_tree,
    }


def _with_candidate_expectation(row, prediction, metric, selection):
    selection["candidate_expectation"] = _candidate_expectation(
        row,
        prediction,
        metric,
        selection,
    )
    return selection


def resolve_local_image_id(image):
    result = run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", "--", str(image or "")],
        timeout=120,
    )
    image_id = str(result.get("stdout") or "").strip().lower()
    if result.get("returncode") != 0 or _IMAGE_ID_RE.fullmatch(image_id) is None:
        return {"ok": False, "status": "image_identity_unavailable", "image_id": ""}
    return {"ok": True, "status": "verified", "image_id": image_id}


def bind_eval_image(row, selection):
    """Bind one evaluation selection to an immutable local image ID."""
    if not selection.get("ok") or _IMAGE_ID_RE.fullmatch(str(selection.get("image_id") or "")):
        return selection
    image = image_for_row(row)
    image_status = ensure_image(image)
    selection["image"] = image
    selection["image_status"] = image_status
    if not image_status.get("ok"):
        selection.update({"ok": False, "status": "blocked_missing_eval_image"})
        return selection
    image_identity = resolve_local_image_id(image)
    selection["image_identity"] = image_identity
    if not image_identity.get("ok"):
        selection.update({"ok": False, "status": "image_identity_unavailable"})
        return selection
    selection["image_id"] = image_identity["image_id"]
    return selection


def probe_gitlink_deletions(
    *,
    task,
    image,
    image_id,
    base_commit,
    source_patch_sha256,
    candidates,
):
    binding = {
        "task": str(task or ""),
        "image": str(image or ""),
        "image_id": str(image_id or "").lower(),
        "base_commit": str(base_commit or "").lower(),
        "source_patch_sha256": str(source_patch_sha256 or "").lower(),
    }
    evidence = {
        "schema": GITLINK_PROBE_SCHEMA,
        **binding,
        "status": "invalid_input",
        "paths": [],
    }
    paths = [str(item.get("path") or "") for item in candidates]
    if (
        not binding["task"]
        or not binding["image"]
        or _IMAGE_ID_RE.fullmatch(binding["image_id"]) is None
        or _OBJECT_ID_RE.fullmatch(binding["base_commit"]) is None
        or re.fullmatch(r"[0-9a-f]{64}", binding["source_patch_sha256"]) is None
        or not paths
        or len(paths) > MAX_GITLINK_PROBE_PATHS
        or len(set(paths)) != len(paths)
        or sum(len(path.encode("utf-8", errors="surrogatepass")) for path in paths)
        > MAX_GITLINK_PROBE_PATH_BYTES
        or any(not path or "\x00" in path for path in paths)
    ):
        return {"ok": False, "status": "invalid_gitlink_probe_input", "probe": evidence}

    container_name = "opencollab-prolite-gitlink-probe-" + uuid.uuid4().hex[:20]
    cidfile = base_run_dir / ("." + container_name + ".cid")
    probe_argv = [
        "docker",
        "run",
        "--rm",
        "--read-only",
        "--network",
        "none",
        "--entrypoint",
        "/bin/bash",
        binding["image_id"],
        "-c",
        _PROBE_SCRIPT,
        "opencollab-gitlink-probe",
        binding["base_commit"],
        "--",
        *paths,
    ]
    command = [
        "timeout",
        "120",
        "docker",
        "run",
        "--rm",
        "--read-only",
        "--name",
        container_name,
        "--cidfile",
        str(cidfile),
        "--label",
        f"{PREFLIGHT_OWNER_LABEL}={owner_nonce}",
        "--label",
        f"{PREFLIGHT_SCHEMA_LABEL}={PREFLIGHT_SCHEMA}",
        "--network",
        "none",
        "--entrypoint",
        "/bin/bash",
        binding["image_id"],
        "-c",
        _PROBE_SCRIPT,
        "opencollab-gitlink-probe",
        binding["base_commit"],
        "--",
        *paths,
    ]
    evidence["probe_argv"] = probe_argv
    evidence["probe_script_sha256"] = hashlib.sha256(_PROBE_SCRIPT.encode("utf-8")).hexdigest()
    evidence["probe_command_sha256"] = hashlib.sha256(
        _canonical_json(probe_argv).encode("utf-8")
    ).hexdigest()
    result = {"returncode": 127, "stdout": "", "stderr": "probe did not start"}
    try:
        result = run(command, timeout=150)
    finally:
        cleanup = cleanup_preflight_container(cidfile, container_name)
    evidence["returncode"] = result.get("returncode")
    evidence["container_cleanup"] = cleanup
    if result.get("returncode") != 0 or cleanup.get("ok") is not True:
        evidence["status"] = "probe_execution_failed"
        return {"ok": False, "status": "gitlink_probe_execution_failed", "probe": evidence}
    try:
        entries = parse_ls_tree_entries(result.get("stdout"))
    except ValueError:
        evidence["status"] = "probe_output_invalid"
        return {"ok": False, "status": "gitlink_probe_output_invalid", "probe": evidence}
    parsed_output = [
        {
            "path": path,
            "base_mode": str(entry.get("base_mode") or ""),
            "base_type": str(entry.get("base_type") or ""),
            "base_oid": str(entry.get("base_oid") or ""),
        }
        for path, entry in sorted(entries.items())
    ]
    evidence["probe_parsed_output"] = parsed_output
    evidence["probe_output_sha256"] = hashlib.sha256(
        _canonical_json(parsed_output).encode("utf-8")
    ).hexdigest()

    verified = []
    for item in candidates:
        path = str(item["path"])
        old_oid = str(item["old_oid"]).lower()
        base = entries.get(path, {})
        probe_status = (
            "verified"
            if base.get("base_mode") == "160000"
            and base.get("base_type") == "commit"
            and base.get("base_oid") == old_oid
            else "mismatch"
        )
        row = {
            "block_index": int(item["block_index"]),
            "path": path,
            "old_oid": old_oid,
            "base_mode": str(base.get("base_mode") or ""),
            "base_type": str(base.get("base_type") or ""),
            "base_oid": str(base.get("base_oid") or ""),
            "probe_status": probe_status,
        }
        evidence["paths"].append(row)
        verified.append(row)
    if any(item["probe_status"] != "verified" for item in verified):
        evidence["status"] = "baseline_mismatch"
        return {"ok": False, "status": "gitlink_probe_baseline_mismatch", "probe": evidence}
    evidence["status"] = "verified"
    return {"ok": True, "status": "verified", "probe": evidence, "verified": verified}


def prepare_eval_patch_selection(row, prediction, metric, runtime_dependency_specs=()):
    source_patch = prediction_patch(prediction)
    model_patch, filtered_paths = filter_model_patch_with_evidence(source_patch)
    candidate_paths = set(patch_paths(source_patch))
    protected_paths = set(patch_paths(str(row.get("test_patch") or "")))
    protected_runtime_roots = {
        str(item.get("root") or "").strip("/")
        for item in runtime_dependency_specs
        if isinstance(item, dict)
        and item.get("candidate_protected") is True
        and str(item.get("root") or "").strip("/")
    }
    for target in parse_literal_list(row.get("FAIL_TO_PASS") or row.get("fail_to_pass")):
        path = target.split("::", 1)[0].split(" | ", 1)[0]
        while path.startswith("./"):
            path = path[2:]
        path = path.lstrip("/")
        if "/" in path or "." in pathlib.PurePosixPath(path).name:
            protected_paths.add(path)
    protected_candidate_paths = {
        path
        for path in candidate_paths & protected_paths
        if not is_eval_control_path(path)
    }
    model_patch, additionally_filtered = filter_patch_paths_with_evidence(
        model_patch,
        protected_candidate_paths,
    )
    filtered_paths = list(dict.fromkeys([*filtered_paths, *additionally_filtered]))
    selection = {
        "ok": True,
        "status": "ready",
        "model_patch": model_patch,
        "source_patch_sha256": patch_sha(source_patch),
        "eval_patch_sha256": patch_sha(model_patch),
        "filtered_patch_paths": filtered_paths,
        "gitlink_probe": None,
        "image": "",
    }
    tampered_paths = sorted(
        path
        for path in candidate_paths
        if is_eval_control_path(path)
        or any(path == root or path.startswith(root + "/") for root in protected_runtime_roots)
    )
    if tampered_paths:
        selection.update(
            ok=False,
            status="candidate_evaluation_surface_tampering",
            tampered_paths=tampered_paths,
        )
        return selection
    if isinstance(metric, dict) and solver_git_snapshot_valid(metric.get("solver_git_snapshot")):
        return _with_candidate_expectation(row, prediction, metric, selection)
    candidates = gitlink_deletion_candidates(model_patch)
    if not candidates:
        return _with_candidate_expectation(row, prediction, metric, selection)
    removed_gitlinks = _trusted_removed_gitlinks(
        row,
        prediction,
        metric,
        source_patch,
        selection["source_patch_sha256"],
    )
    eligible_candidates = [
        item for item in candidates if (item["path"], item["old_oid"]) in (removed_gitlinks or set())
    ]
    if not eligible_candidates:
        return _with_candidate_expectation(row, prediction, metric, selection)
    bind_eval_image(row, selection)
    if not selection.get("ok"):
        return selection
    probe = probe_gitlink_deletions(
        task=row.get("instance_id"),
        image=selection["image"],
        image_id=selection["image_id"],
        base_commit=str(row.get("base_commit") or row.get("commit") or "").strip(),
        source_patch_sha256=selection["source_patch_sha256"],
        candidates=eligible_candidates,
    )
    selection["gitlink_probe"] = probe["probe"]
    if not probe.get("ok"):
        selection.update({"ok": False, "status": probe["status"]})
        return selection
    model_patch, gitlink_paths = filter_verified_gitlink_deletions(
        model_patch,
        probe["verified"],
    )
    selection.update(
        {
            "model_patch": model_patch,
            "eval_patch_sha256": patch_sha(model_patch),
            "filtered_patch_paths": [*filtered_paths, *gitlink_paths],
        }
    )
    return _with_candidate_expectation(row, prediction, metric, selection)


__all__ = [
    "GITLINK_PROBE_SCHEMA",
    "LEGACY_GITLINK_AUDIT_SCHEMA",
    "bind_eval_image",
    "prepare_eval_patch_selection",
    "probe_gitlink_deletions",
    "resolve_local_image_id",
]
