from __future__ import annotations

import subprocess
from pathlib import Path

from opencollab_eval.generation.gen_prediction_constants import _ACTIVATE


def _prefix_for(tmp_path: Path) -> tuple[str, Path, Path]:
    activate = tmp_path / "miniconda3/bin/activate"
    environment = tmp_path / "miniconda3/envs/testbed"
    prefix = _ACTIVATE.replace(
        "/opt/miniconda3/bin/activate", str(activate)
    ).replace("/opt/miniconda3/envs/testbed", str(environment))
    return prefix, activate, environment


def test_failed_testbed_activation_stops_following_shell_commands(tmp_path: Path) -> None:
    prefix, activate, environment = _prefix_for(tmp_path)
    activate.parent.mkdir(parents=True)
    environment.mkdir(parents=True)
    activate.write_text("return 1\n")
    marker = tmp_path / "must-not-run"

    result = subprocess.run(
        ["bash", "-lc", f"{prefix}\nprintf ignored; printf reached > {marker}"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 86
    assert not marker.exists()
    assert "testbed activation failed" in result.stderr


def test_successful_testbed_activation_runs_following_command(tmp_path: Path) -> None:
    prefix, activate, environment = _prefix_for(tmp_path)
    activate.parent.mkdir(parents=True)
    environment.mkdir(parents=True)
    activate.write_text("export CONDA_DEFAULT_ENV=testbed\nreturn 0\n")
    marker = tmp_path / "activated"

    result = subprocess.run(
        [
            "bash",
            "-lc",
            f"{prefix}\nprintf '%s' \"$CONDA_DEFAULT_ENV\" > {marker}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert marker.read_text() == "testbed"


def test_non_conda_image_keeps_native_toolchain_command(tmp_path: Path) -> None:
    prefix, _activate, _environment = _prefix_for(tmp_path)
    marker = tmp_path / "native-toolchain"

    result = subprocess.run(
        ["bash", "-lc", f"{prefix}\nprintf '%s' go > {marker}"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert marker.read_text() == "go"


def test_missing_required_activation_script_stops_command(tmp_path: Path) -> None:
    prefix, _activate, environment = _prefix_for(tmp_path)
    environment.mkdir(parents=True)
    result = subprocess.run(
        ["bash", "-lc", f"{prefix}\nprintf should-not-run"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 86
    assert result.stdout == ""
    assert "Environment preparation failed" in result.stderr


def test_success_exit_without_activation_is_rejected(tmp_path: Path) -> None:
    import os

    prefix, activate, environment = _prefix_for(tmp_path)
    activate.parent.mkdir(parents=True)
    environment.mkdir(parents=True)
    activate.write_text("unset CONDA_DEFAULT_ENV\nreturn 0\n")
    result = subprocess.run(
        ["bash", "-lc", f"{prefix}\nprintf should-not-run"],
        env={k: v for k, v in os.environ.items() if k != "CONDA_DEFAULT_ENV"},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 86
    assert result.stdout == ""
    assert "activation was not confirmed" in result.stderr


def test_preparation_failure_is_reported_before_solver(monkeypatch) -> None:
    import pytest

    from opencollab_eval.generation import gen_prediction_docker as docker

    commands = []

    def failed_activation(*args):
        commands.append(args)
        return subprocess.CompletedProcess(args, 86, "", "testbed activation failed")

    monkeypatch.setattr(docker, "_docker", failed_activation)
    with pytest.raises(RuntimeError, match="solver environment preparation failed"):
        docker.prepare_testbed_environment("fixture-container")
    assert commands == [("exec", "fixture-container", "bash", "-lc", _ACTIVATE)]
