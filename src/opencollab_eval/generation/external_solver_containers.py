"""Trusted cleanup for external solver containers recorded by identity labels."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

CID_RE = re.compile(r"[0-9a-f]{64}")
TASK_ID_RE = re.compile(r"solver-[0-9a-f]{32}")
EXPECTED_OWNERS = {
    "test-container.id": "claude-code-external",
    "runtime-container.id": "claude-code-runtime",
    "relay-container.id": "claude-code-relay",
    "gateway-container.id": "claude-code-gateway",
}
EXPECTED_OWNER_VALUES = {*EXPECTED_OWNERS.values(), "claude-code-probe"}


def _docker(command: list[str], timeout: int) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _detail(result: subprocess.CompletedProcess[str] | None) -> str:
    if result is None:
        return "docker command failed to start or timed out"
    return (result.stderr or result.stdout).strip()


def _explicitly_absent(result: subprocess.CompletedProcess[str] | None) -> bool:
    if result is None or result.returncode == 0:
        return False
    detail = _detail(result).lower()
    return "no such container" in detail or "no such object" in detail


def _network_explicitly_absent(
    result: subprocess.CompletedProcess[str] | None,
    expected_name: str,
) -> bool:
    if _explicitly_absent(result):
        return True
    if result is None or result.returncode == 0:
        return False
    detail = _detail(result).lower()
    return "no such network" in detail or f"network {expected_name.lower()} not found" in detail


def _inspect_labels(cid: str) -> tuple[str, dict[str, str], str]:
    result = _docker(
        ["docker", "inspect", "--format", "{{json .Config.Labels}}", cid],
        20,
    )
    detail = _detail(result)
    if _explicitly_absent(result):
        return "absent", {}, detail
    if result is None or result.returncode != 0:
        return "error", {}, detail
    try:
        labels = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "error", {}, "docker inspect returned malformed labels"
    return "present", labels if isinstance(labels, dict) else {}, detail


def _query_task_containers(solver_task_id: str) -> tuple[list[str] | None, str]:
    result = _docker(
        [
            "docker",
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            f"label=opencollab.solver_task_id={solver_task_id}",
        ],
        20,
    )
    if result is None or result.returncode != 0:
        return None, _detail(result)
    cids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if any(CID_RE.fullmatch(cid) is None for cid in cids):
        return None, "docker ps returned an invalid container identity"
    return list(dict.fromkeys(cids)), ""


def _cleanup_network(network_name: object, solver_task_id: str) -> dict[str, Any]:
    expected_name = f"oc-claude-net-{solver_task_id}"
    if network_name is None:
        return {"proven": True, "status": "not_declared"}
    if network_name != expected_name:
        return {"proven": False, "status": "identity_mismatch"}
    inspect = _docker(
        ["docker", "network", "inspect", "--format", "{{json .Labels}}", expected_name],
        20,
    )
    if _network_explicitly_absent(inspect, expected_name):
        return {"proven": True, "status": "already_absent", "name": expected_name}
    if inspect is None or inspect.returncode != 0:
        return {"proven": False, "status": "inspect_failed", "error": _detail(inspect)}
    try:
        labels = json.loads(inspect.stdout)
    except json.JSONDecodeError:
        return {"proven": False, "status": "inspect_failed", "error": "malformed labels"}
    if not isinstance(labels, dict) or labels.get("opencollab.owner") != "claude-code-network" or labels.get(
        "opencollab.solver_task_id"
    ) != solver_task_id:
        return {"proven": False, "status": "owner_mismatch", "name": expected_name}
    removed = _docker(["docker", "network", "rm", expected_name], 30)
    post = _docker(["docker", "network", "inspect", expected_name], 20)
    absent = _network_explicitly_absent(post, expected_name)
    remove_absent = _network_explicitly_absent(removed, expected_name)
    success = absent and bool(
        removed is not None and (removed.returncode == 0 or remove_absent)
    )
    return {
        "proven": success,
        "status": "removed" if success else "cleanup_failed",
        "name": expected_name,
        "absent_verified": absent,
        "remove_detail": _detail(removed),
        "post_remove_detail": _detail(post),
    }


def _remove_container(
    cid: str,
    *,
    solver_task_id: str,
    expected_owner: str | None,
    cidfile: str | None,
) -> tuple[bool, dict[str, Any]]:
    record: dict[str, Any] = {"cid": cid, "cidfile": cidfile, "source": "cidfile" if cidfile else "label_query"}
    status, labels, detail = _inspect_labels(cid)
    if status == "absent":
        record["status"] = "already_absent"
        return True, record
    if status != "present":
        record.update(status="inspect_failed", error=detail)
        return False, record
    owner = labels.get("opencollab.owner")
    if (
        owner not in EXPECTED_OWNER_VALUES
        or (expected_owner is not None and owner != expected_owner)
        or labels.get("opencollab.solver_task_id") != solver_task_id
    ):
        record["status"] = "owner_mismatch"
        return False, record
    removed = _docker(["docker", "rm", "-f", cid], 30)
    absent_status, _labels, absent_detail = _inspect_labels(cid)
    absent = absent_status == "absent"
    remove_ok = bool(removed is not None and removed.returncode == 0)
    raced_auto_remove = _explicitly_absent(removed) and absent
    success = absent and (remove_ok or raced_auto_remove)
    record.update(
        status="removed" if success else "cleanup_failed",
        owner=owner,
        remove_returncode=None if removed is None else removed.returncode,
        remove_detail=_detail(removed),
        absent_verified=absent,
        post_remove_inspect_error=None if absent else absent_detail,
    )
    return success, record


def cleanup_external_solver_containers(output_dir: Path) -> dict[str, Any]:
    marker = output_dir / "external_solver.required.json"
    if not marker.exists():
        return {"proven": True, "containers": [], "label_query": "not_required"}
    try:
        required = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"proven": False, "error": f"invalid external solver marker: {exc}"}
    solver_task_id = required.get("solver_task_id") if isinstance(required, dict) else None
    if not isinstance(solver_task_id, str) or TASK_ID_RE.fullmatch(solver_task_id) is None:
        return {"proven": False, "error": "external solver marker lacks valid task identity"}

    records: list[dict[str, Any]] = []
    references: dict[str, tuple[str | None, str | None]] = {}
    proven = True
    for filename, expected_owner in EXPECTED_OWNERS.items():
        path = output_dir / filename
        if not path.exists():
            records.append({"cidfile": filename, "status": "cidfile_missing"})
            continue
        try:
            cid = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            records.append({"cidfile": filename, "status": "cidfile_unreadable", "error": str(exc)})
            continue
        if CID_RE.fullmatch(cid) is None:
            records.append({"cidfile": filename, "status": "invalid_cid"})
            continue
        references[cid] = (expected_owner, filename)

    discovered, query_error = _query_task_containers(solver_task_id)
    if discovered is None:
        return {
            "proven": False,
            "containers": records,
            "label_query": "failed",
            "label_query_error": query_error,
        }
    for cid in discovered:
        references.setdefault(cid, (None, None))
    for cid, (expected_owner, cidfile) in references.items():
        cleaned, record = _remove_container(
            cid,
            solver_task_id=solver_task_id,
            expected_owner=expected_owner,
            cidfile=cidfile,
        )
        proven = proven and cleaned
        records.append(record)

    remaining, final_query_error = _query_task_containers(solver_task_id)
    final_absence = remaining == []
    proven = proven and final_absence
    if remaining is None:
        proven = False
    if proven:
        for record in records:
            filename = record.get("cidfile")
            if not isinstance(filename, str):
                continue
            path = output_dir / filename
            if not path.exists():
                continue
            try:
                path.unlink()
            except OSError as exc:
                proven = False
                record["cidfile_removed"] = False
                record["cidfile_remove_error"] = str(exc)
            else:
                record["cidfile_removed"] = True
    network = _cleanup_network(required.get("network_name"), solver_task_id)
    proven = proven and network["proven"] is True
    return {
        "proven": proven,
        "solver_task_id": solver_task_id,
        "containers": records,
        "label_query": "absent" if final_absence else "not_absent",
        "remaining_container_ids": remaining,
        "label_query_error": final_query_error or None,
        "network": network,
    }


__all__ = ["cleanup_external_solver_containers"]
