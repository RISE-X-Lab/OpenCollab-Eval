"""Host entry points for preserving trusted image development dependencies."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from pathlib import Path

from .gen_prediction_constants import DOCKER_WORKDIR
from .gen_prediction_docker import _check_docker
from .gen_prediction_snapshot import _docker_with_stdin, _install_snapshot_helper

_HELPER = "/tmp/opencollab_generation_runtime_dependencies.py"


@dataclass(frozen=True)
class SolverRuntimeDependencies:
    store: str
    roots: tuple[str, ...]
    workspace: str = DOCKER_WORKDIR


def _run(container_id: str, action: str, workspace: str, argument: str, payload: str = "") -> str:
    result = _docker_with_stdin(
        "exec",
        "-i",
        "-w",
        "/tmp",
        container_id,
        "python3",
        _HELPER,
        action,
        workspace,
        argument,
        input_text=payload,
    )
    _check_docker(result, f"solver runtime dependency {action}")
    return result.stdout


def _install_helpers(container_id: str) -> None:
    source = Path(__file__).parent
    for local, remote in (
        (source / "generation_runtime_dependencies.py", _HELPER),
        (source.parent / "engine" / "eval_runtime_dependencies.py", "/tmp/opencollab_eval_runtime_dependencies.py"),
    ):
        _install_snapshot_helper(container_id, local, remote)


def stash_solver_runtime_dependencies(
    container_id: str,
    expected_base_commit: str,
    *,
    workspace: str = DOCKER_WORKDIR,
) -> SolverRuntimeDependencies:
    _install_helpers(container_id)
    store = f"/tmp/opencollab-generation-runtime-{secrets.token_hex(8)}"
    roots = json.loads(_run(container_id, "stash", workspace, store, expected_base_commit + "\n"))
    return SolverRuntimeDependencies(store, tuple(roots), workspace)


def restore_solver_runtime_dependencies(container_id: str, state: SolverRuntimeDependencies) -> None:
    _run(container_id, "restore", state.workspace, state.store)
    if {"package.json", "package-lock.json", "node_modules", "config.json"}.issubset(state.roots):
        check = _docker_with_stdin(
            "exec",
            "-i",
            "-w",
            state.workspace,
            container_id,
            "python3",
            "-c",
            "import json,pathlib; print(json.loads(pathlib.Path('package.json').read_text()).get('name',''))",
            input_text="",
        )
        _check_docker(check, "public application runtime identification")
        if check.stdout.strip() == "nodebb":
            from opencollab_eval.engine.swe_v1_remote_commands import prolite_service_bootstrap

            service = _docker_with_stdin(
                "exec",
                "-i",
                "-w",
                state.workspace,
                container_id,
                "bash",
                "-s",
                input_text=prolite_service_bootstrap({"repo": "NodeBB/NodeBB"}),
            )
            _check_docker(service, "NodeBB public Redis preparation")
            ping = _docker_with_stdin(
                "exec",
                "-i",
                container_id,
                "redis-cli",
                "-h",
                "127.0.0.1",
                "-p",
                "6379",
                "ping",
                input_text="",
            )
            _check_docker(ping, "NodeBB public Redis readiness")
            if ping.stdout.strip() != "PONG":
                raise RuntimeError("NodeBB public Redis did not return PONG")


def remove_solver_runtime_dependencies(container_id: str, state: SolverRuntimeDependencies) -> None:
    _install_helpers(container_id)
    _run(container_id, "remove", state.workspace, "-", json.dumps(state.roots))
