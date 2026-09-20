"""Every docker call in the teardown path obeys OPENCOLLAB_DOCKER_TIMEOUT.

Three call sites used to pin 30 s and so opt out of the knob silently. That cost
a whole cell: at 36 concurrent runs `docker rm -f` took longer than 30 s, every
finished run raised `technical container cleanup failed`, and five successful
runs in a row were recorded as failures with no prediction written -- the agent
had already done its work.
"""

from __future__ import annotations

import subprocess

import pytest

gpd = pytest.importorskip("opencollab_eval.generation.gen_prediction_docker")


def _capture_timeouts(monkeypatch, env_value):
    seen: list[float | None] = []

    def fake_run(args, **kwargs):
        seen.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setenv("OPENCOLLAB_DOCKER_TIMEOUT", env_value)
    monkeypatch.setattr(gpd.subprocess, "run", fake_run)
    return seen


@pytest.mark.parametrize("env_value,expected", [("300", 300.0), ("45", 45.0)])
def test_container_removal_uses_the_configured_docker_timeout(monkeypatch, env_value, expected):
    seen = _capture_timeouts(monkeypatch, env_value)
    gpd.remove_container("cid")
    # `rm -f` then the absence check that proves the removal; both are docker calls
    assert seen and set(seen) == {expected}


@pytest.mark.parametrize("env_value,expected", [("300", 300.0), ("45", 45.0)])
def test_absence_check_uses_the_configured_docker_timeout(monkeypatch, env_value, expected):
    seen = _capture_timeouts(monkeypatch, env_value)
    gpd._container_is_absent("cid")
    assert seen == [expected]


@pytest.mark.parametrize("env_value,expected", [("300", 300.0), ("45", 45.0)])
def test_owner_label_read_uses_the_configured_docker_timeout(monkeypatch, env_value, expected):
    seen = _capture_timeouts(monkeypatch, env_value)
    gpd._container_owner_label_state("cid", "token")
    assert seen == [expected]


def test_no_docker_call_in_this_module_pins_its_own_timeout():
    """A new call site that pins a number would reintroduce the same defect."""
    import inspect

    source = inspect.getsource(gpd)
    offenders = [
        line.strip()
        for line in source.splitlines()
        if "timeout=" in line and "subprocess.run" not in line and "timeout=timeout" not in line
    ]
    assert offenders == [], offenders
