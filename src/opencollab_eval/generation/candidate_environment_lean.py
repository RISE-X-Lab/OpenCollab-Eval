"""Candidate environment adapter for the lean validation council workflow."""

from __future__ import annotations

import json
import shlex
from pathlib import Path, PurePosixPath

try:
    from .candidate_runtime import command_prefix
except ImportError:
    from candidate_runtime import command_prefix


def image_activation_prefix(container_id, activation=""):
    """Restore the image PATH after the environment's login shell initialization."""
    from .gen_prediction_docker import _check_docker, _docker, prepare_testbed_environment

    prepare_testbed_environment(container_id)
    result = _docker("inspect", "--format", "{{json .Config.Env}}", container_id)
    _check_docker(result, "read image environment")
    entries = json.loads(result.stdout) or []
    paths = [value[5:] for value in entries if isinstance(value, str) and value.startswith("PATH=")]
    if not paths:
        return activation
    return "export PATH=" + shlex.quote(paths[-1]) + "\n" + activation


def image_helper_python(container_id):
    """Resolve the control-plane Python before a login shell changes PATH."""
    from .gen_prediction_docker import _check_docker, _docker

    result = _docker(
        "exec",
        container_id,
        "python3",
        "-c",
        "import os, sys; print(os.path.realpath(sys.executable))",
    )
    _check_docker(result, "resolve candidate runtime Python")
    executable = result.stdout.strip()
    if (
        not executable
        or "\n" in executable
        or "\r" in executable
        or not PurePosixPath(executable).is_absolute()
    ):
        raise RuntimeError("candidate runtime Python must resolve to one absolute container path")
    return executable


def install_candidate_environment(container_id, solver_runtime, activation=""):
    """Prepare ignored dependencies for the lean workflow's candidate worktree."""
    from .gen_prediction_config import _dependency_preparation_timeout_from_env
    from .gen_prediction_docker import _check_docker
    from .gen_prediction_snapshot import _docker_with_stdin, _install_snapshot_helper

    activation = image_activation_prefix(container_id, activation)
    helper_python = image_helper_python(container_id)
    helper = "/tmp/opencollab_candidate_runtime.py"
    store = solver_runtime.store + "-candidates"
    _install_snapshot_helper(container_id, Path(__file__).with_name("candidate_runtime.py"), helper)
    result = _docker_with_stdin(
        "exec",
        "-i",
        "-w",
        "/tmp",
        container_id,
        helper_python,
        helper,
        "prepare",
        solver_runtime.workspace,
        store,
        input_text=json.dumps(solver_runtime.roots),
        timeout=_dependency_preparation_timeout_from_env(),
    )
    _check_docker(result, "candidate runtime dependency preparation")
    return command_prefix(
        store,
        solver_runtime.workspace,
        python=helper_python,
        helper=helper,
        activation=activation,
    )


__all__ = ["image_activation_prefix", "image_helper_python", "install_candidate_environment"]
