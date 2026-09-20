"""Collect best-effort Docker and cgroup evidence before removing a container."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


def collect_resource_evidence(reference: str, inspect: Callable[[], Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "schema": "opencollab.container_termination_evidence.v1",
        "collected_epoch": time.time(),
        "capture_status": "unavailable",
        "container_reference": reference,
        "container_id": None,
        "state": {"oom_killed": None, "exit_code": None, "status": None},
        "host_config": {"memory_bytes": None, "memory_swap_bytes": None},
        "cgroup": {"path": None, "memory_events": None, "memory_peak_bytes": None},
        "errors": [],
    }
    try:
        result = inspect()
        if result is None or result.returncode:
            detail = "unavailable" if result is None else (result.stderr or result.stdout or "")[-1000:]
            raise ValueError("docker inspect failed " + detail)
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError("docker inspect did not return one container")
        container = rows[0]
        state, host = container.get("State"), container.get("HostConfig")
        if not isinstance(state, dict) or not isinstance(host, dict):
            raise ValueError("docker inspect omitted resource state")
    except Exception as error:
        evidence["errors"].append(f"{type(error).__name__}: {error}")
        return evidence

    evidence["capture_status"] = "captured"
    evidence["container_id"] = container.get("Id")
    evidence["state"] = {
        "oom_killed": state.get("OOMKilled"),
        "exit_code": state.get("ExitCode"),
        "status": state.get("Status"),
    }
    evidence["host_config"] = {
        "memory_bytes": host.get("Memory"),
        "memory_swap_bytes": host.get("MemorySwap"),
    }
    paths: list[Path] = []
    pid = state.get("Pid")
    if type(pid) is int and pid > 0:
        try:
            for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
                fields = line.split(":", 2)
                if len(fields) == 3 and fields[0] == "0" and fields[2].startswith("/"):
                    paths.append(Path("/sys/fs/cgroup") / fields[2].lstrip("/"))
        except OSError as error:
            evidence["errors"].append(f"cannot read process cgroup: {error}")
    cid = evidence["container_id"]
    if isinstance(cid, str) and len(cid) == 64 and all(c in "0123456789abcdef" for c in cid):
        paths.append(Path("/sys/fs/cgroup/system.slice") / f"docker-{cid}.scope")
    for path in dict.fromkeys(paths):
        events, peak = path / "memory.events", path / "memory.peak"
        try:
            present = events.is_file() or peak.is_file()
        except OSError as error:
            evidence["errors"].append(f"cannot inspect cgroup: {error}")
            continue
        if not present:
            continue
        evidence["cgroup"]["path"] = str(path)
        try:
            evidence["cgroup"]["memory_events"] = {
                key: int(value)
                for key, value in (line.split(None, 1) for line in events.read_text().splitlines())
            }
        except (OSError, ValueError) as error:
            evidence["errors"].append(f"cannot read {events}: {error}")
        try:
            evidence["cgroup"]["memory_peak_bytes"] = int(peak.read_text().strip())
        except (OSError, ValueError) as error:
            evidence["errors"].append(f"cannot read {peak}: {error}")
        break
    if evidence["cgroup"]["path"] is None:
        evidence["errors"].append("readable cgroup memory files not found")
    return evidence
