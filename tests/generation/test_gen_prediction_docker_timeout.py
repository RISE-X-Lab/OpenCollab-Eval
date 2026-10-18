"""Apply OPENCOLLAB_DOCKER_TIMEOUT to every teardown call.

At high concurrency, container removal can exceed a hardcoded 30-second limit
and turn completed solver work into a cleanup failure without a prediction.
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
