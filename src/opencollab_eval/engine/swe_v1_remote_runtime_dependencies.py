"""Controller-side identity probes for eval-only runtime dependencies."""

# ruff: noqa: F403, F405

import shutil

from opencollab_eval.engine.swe_v1_remote_records import *
from opencollab_eval.engine.swe_v1_remote_state import *


def _run_bounded(command, timeout, limit=8192):
    """Run one owned process with a hard combined-output memory bound."""
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        return {"returncode": 127, "stdout": "", "stderr": str(exc)}
    ACTIVE_CHILD_PGIDS.add(proc.pid)
    captured = bytearray()
    overflow = threading.Event()

    def reader():
        while proc.stdout is not None:
            chunk = proc.stdout.read(4096)
            if not chunk:
                return
            remaining = limit + 1 - len(captured)
            captured.extend(chunk[:remaining])
            if len(captured) > limit:
                overflow.set()
                return

    reader_thread = threading.Thread(target=reader, name=f"runtime-probe-{proc.pid}", daemon=True)
    reader_thread.start()
    deadline = time.monotonic() + timeout
    while proc.poll() is None and not overflow.is_set() and time.monotonic() < deadline:
        overflow.wait(0.05)
    if overflow.is_set():
        quiesced = terminate_process_group_bounded(proc)
        returncode, error = 125, "runtime dependency probe output exceeded 8192 bytes"
    elif proc.poll() is None:
        quiesced = terminate_process_group_bounded(proc)
        returncode, error = 124, "runtime dependency probe timed out"
    else:
        returncode = proc.wait()
        quiesced = ensure_process_group_quiesced_after_wait(proc)
        error = "" if quiesced else "runtime dependency probe cleanup failed"
    if quiesced:
        ACTIVE_CHILD_PGIDS.discard(proc.pid)
    reader_thread.join(timeout=1)
    if proc.stdout is not None:
        proc.stdout.close()
    output = bytes(captured[:limit]).decode("utf-8", errors="replace")
    return {"returncode": returncode, "stdout": output, "stderr": error}


def image_runtime_dependency_identities(image, specs):
    """Read ignored file dependency identities from an immutable fresh image."""
    file_roots = [item["root"] for item in specs if item.get("kind") == "file"]
    document = {
        "schema": "opencollab.runtime_dependency_identities.v1",
        "image_id": image,
        "entries": [],
    }
    if not file_roots:
        return {"ok": True, "document": document}
    script = r"""
import hashlib
import json
import os
import pathlib
import subprocess
import sys

roots = json.loads(sys.argv[1])
repo = next(
    (pathlib.Path(value) for value in ("/app", "/testbed", "/workspace", "/repo", "/src")
     if (pathlib.Path(value) / ".git").is_dir()),
    None,
)
if repo is None:
    raise SystemExit("missing evaluation repository")
entries = []
git_env = dict(os.environ)
git_env.update({
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.directory",
    "GIT_CONFIG_VALUE_0": str(repo),
})
for root in roots:
    path = repo / root
    tracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", root],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=git_env,
    ).returncode == 0
    ignored = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "-q", "--", root],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=git_env,
    ).returncode == 0
    if tracked or not path.exists() or not ignored:
        continue
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise SystemExit("runtime dependency file is not a bounded regular file")
    entries.append({"root": root, "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
print(json.dumps(entries, sort_keys=True))
"""
    container_name = "opencollab-prolite-preflight-" + uuid.uuid4().hex[:24]
    cidfile = base_run_dir / ("." + container_name + ".cid")
    timeout_prefix = ["timeout", "120"] if shutil.which("timeout") else []
    command = [
        *timeout_prefix, "docker", "run", "--rm", "--name", container_name,
        "--cidfile", str(cidfile),
        "--label", f"{PREFLIGHT_OWNER_LABEL}={owner_nonce}",
        "--label", f"{PREFLIGHT_SCHEMA_LABEL}={PREFLIGHT_SCHEMA}",
        "--network", "none", "--entrypoint", "python3", image,
        "-c", script, json.dumps(file_roots),
    ]
    try:
        result = _run_bounded(command, timeout=150)
    finally:
        cleanup = cleanup_preflight_container(cidfile, container_name)
    try:
        entries = json.loads(result["stdout"])
    except (TypeError, json.JSONDecodeError):
        entries = None
    valid = (
        result["returncode"] == 0
        and cleanup.get("ok") is True
        and isinstance(entries, list)
        and len(entries) <= len(file_roots)
        and all(
            isinstance(item, dict)
            and set(item) == {"root", "content_sha256"}
            and item["root"] in file_roots
            and re.fullmatch(r"[0-9a-f]{64}", item["content_sha256"])
            for item in entries
        )
        and len({item["root"] for item in entries}) == len(entries)
    )
    document["entries"] = entries if valid else []
    return {
        "ok": valid,
        "document": document,
        "details": (result["stderr"] or result["stdout"])[-1000:],
        "container_cleanup": cleanup,
    }


def prepare_runtime_dependency_identities(
    image,
    specs,
    row,
    prediction,
    eval_spec_sha256,
    summary_path,
):
    """Probe a pinned image and build a terminal result when identity is unavailable."""
    probe = image_runtime_dependency_identities(image, specs)
    if probe.get("ok"):
        return {"ok": True, "document": probe["document"]}
    task = row["instance_id"]
    summary = {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": "technical_eval_failed",
        "task": task,
        "resolved": False,
        "patch_sha256": row_patch_sha(prediction),
        "record_id": row_record_id(prediction),
        "eval_spec_sha256": eval_spec_sha256,
        "eval_image_id": image,
        "technical_reasons": ["runtime_dependency_identity_probe"],
        "runtime_dependency_identity_probe": probe,
        "executed": False,
    }
    write_json(summary_path, summary)
    return {
        "ok": False,
        "result": {
            "status": "technical_eval_failed",
            "task": task,
            "summary": summary,
            "executed": False,
        },
    }


def prepare_eval_runtime(
    row,
    prediction,
    patch_selection,
    specs,
    eval_spec_sha256,
    summary_path,
):
    """Resolve the immutable image and its controller-owned file identities."""
    image = patch_selection.get("image_id") or patch_selection.get("image") or image_for_row(row)
    image_status = patch_selection.get("image_status") or ensure_image(image)
    if not image_status.get("ok"):
        return {
            "ok": False,
            "result": {
                "status": "blocked_missing_eval_image",
                "task": row["instance_id"],
                "image_status": image_status,
                "executed": False,
                "eval_patch_sha256": patch_selection["eval_patch_sha256"],
            },
        }
    identity = prepare_runtime_dependency_identities(
        image, specs, row, prediction, eval_spec_sha256, summary_path
    )
    if not identity["ok"]:
        return identity
    return {"ok": True, "image": image, "document": identity["document"]}


__all__ = [name for name in globals() if not name.startswith("__")]
