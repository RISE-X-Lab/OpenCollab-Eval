"""Host-side eval integration using the existing Docker helper transport."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

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


def install_candidate_environment(container_id, solver_runtime, activation=""):
    """Call after existing dependency restore and before creating the workflow."""
    from .gen_prediction_docker import _check_docker
    from .gen_prediction_snapshot import _docker_with_stdin, _install_snapshot_helper

    activation = image_activation_prefix(container_id, activation)
    helper = "/tmp/opencollab_candidate_runtime.py"
    store = solver_runtime.store + "-candidates"
    _install_snapshot_helper(container_id, Path(__file__).with_name("candidate_runtime.py"), helper)
    result = _docker_with_stdin(
        "exec",
        "-i",
        "-w",
        "/tmp",
        container_id,
        "python3",
        helper,
        "prepare",
        solver_runtime.workspace,
        store,
        input_text=json.dumps(solver_runtime.roots),
    )
    _check_docker(result, "candidate runtime dependency preparation")
    return command_prefix(store, solver_runtime.workspace, helper=helper, activation=activation)
