"""Trusted official-image probes for generated Gitlink deletion artifacts."""

# ruff: noqa: F403, F405

from __future__ import annotations

import hashlib
import json

from opencollab_eval.engine.swe_v1_remote_records import *
from opencollab_eval.patch_gitlinks import *

GITLINK_PROBE_SCHEMA = "opencollab.prolite_gitlink_probe.v1"
MAX_GITLINK_PROBE_PATHS = 1024
MAX_GITLINK_PROBE_PATH_BYTES = 128 * 1024
_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PROBE_SCRIPT = r"""
set -eu
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


def resolve_local_image_id(image):
    result = run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", "--", str(image or "")],
        timeout=120,
    )
    image_id = str(result.get("stdout") or "").strip().lower()
    if result.get("returncode") != 0 or _IMAGE_ID_RE.fullmatch(image_id) is None:
        return {"ok": False, "status": "image_identity_unavailable", "image_id": ""}
    return {"ok": True, "status": "verified", "image_id": image_id}


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
    evidence["probe_script_sha256"] = hashlib.sha256(_PROBE_SCRIPT.encode("utf-8")).hexdigest()
    evidence["probe_command_sha256"] = hashlib.sha256(
        json.dumps(command, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    result = {"returncode": 127, "stdout": "", "stderr": "probe did not start"}
    try:
        result = run(command, timeout=150)
    finally:
        cleanup = cleanup_preflight_container(cidfile, container_name)
    evidence["returncode"] = result.get("returncode")
    evidence["container_cleanup"] = cleanup
    evidence["probe_output_sha256"] = hashlib.sha256(
        str(result.get("stdout") or "").encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    if result.get("returncode") != 0 or cleanup.get("ok") is not True:
        evidence["status"] = "probe_execution_failed"
        return {"ok": False, "status": "gitlink_probe_execution_failed", "probe": evidence}
    try:
        entries = parse_ls_tree_entries(result.get("stdout"))
    except ValueError:
        evidence["status"] = "probe_output_invalid"
        return {"ok": False, "status": "gitlink_probe_output_invalid", "probe": evidence}

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


def prepare_eval_patch_selection(row, prediction, metric):
    source_patch = prediction_patch(prediction)
    model_patch, filtered_paths = filter_model_patch_with_evidence(source_patch)
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
    candidates = gitlink_deletion_candidates(model_patch)
    if not candidates:
        return selection
    if not current_generation_proof_valid(metric, source_patch):
        selection.update(
            {
                "ok": False,
                "status": "gitlink_probe_untrusted_generation",
                "gitlink_probe": {
                    "schema": GITLINK_PROBE_SCHEMA,
                    "status": "untrusted_generation",
                    "task": str(row.get("instance_id") or ""),
                    "source_patch_sha256": selection["source_patch_sha256"],
                    "paths": [
                        {
                            "path": item["path"],
                            "old_oid": item["old_oid"],
                            "base_oid": "",
                            "probe_status": "not_run",
                        }
                        for item in candidates
                    ],
                },
            }
        )
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
    image_id = image_identity["image_id"]
    selection["image_id"] = image_id
    probe = probe_gitlink_deletions(
        task=row.get("instance_id"),
        image=image,
        image_id=image_id,
        base_commit=str(row.get("base_commit") or row.get("commit") or "").strip(),
        source_patch_sha256=selection["source_patch_sha256"],
        candidates=candidates,
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
    return selection


__all__ = [
    "GITLINK_PROBE_SCHEMA",
    "prepare_eval_patch_selection",
    "probe_gitlink_deletions",
    "resolve_local_image_id",
]
