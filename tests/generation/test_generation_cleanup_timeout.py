"""Container teardown uses the operator's timeout and retains ownership checks."""

from __future__ import annotations

import subprocess

import pytest

from opencollab_eval.generation import gen_prediction_docker as docker


def test_owned_cleanup_uses_configured_timeout_through_absence_check(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_DOCKER_TIMEOUT", "120.5")
    owner_token = "a" * 32
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        # Simulate a daemon operation that completes after the old 30s cap.
        if kwargs["timeout"] < 45:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if argv[1:3] == ["inspect", "--type"] and "--format" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=owner_token + "\n", stderr="")
        if argv[1] == "rm":
            return subprocess.CompletedProcess(argv, 0, stdout="owned\n", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Error: No such object: owned")

    monkeypatch.setattr(docker.subprocess, "run", run)

    assert docker._remove_labeled_container("owned", owner_token, foreign_proves_absence=False) is True
    assert [argv[1] for argv, _ in calls] == ["inspect", "rm", "inspect"]
    assert all(timeout == 120.5 for _, timeout in calls)


@pytest.mark.parametrize("stage", ["owner", "remove", "absence"])
def test_cleanup_timeout_still_requires_owner_and_absence_evidence(monkeypatch, stage):
    monkeypatch.setenv("OPENCOLLAB_DOCKER_TIMEOUT", "12.5")
    owner_token = "b" * 32
    calls = []

    def run(argv, **kwargs):
        calls.append(argv[1])
        current = "owner" if "--format" in argv else "remove" if argv[1] == "rm" else "absence"
        if current == stage:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, stdout=owner_token + "\n", stderr="")

    monkeypatch.setattr(docker.subprocess, "run", run)

    assert docker._remove_labeled_container("owned", owner_token, foreign_proves_absence=False) is False
    assert calls == {
        "owner": ["inspect"],
        "remove": ["inspect", "rm"],
        "absence": ["inspect", "rm", "inspect"],
    }[stage]


def test_foreign_owner_is_retained_with_configured_timeout(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_DOCKER_TIMEOUT", "120.5")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="foreign-owner\n", stderr="")

    monkeypatch.setattr(docker.subprocess, "run", run)

    assert docker._remove_labeled_container("owned", "a" * 32, foreign_proves_absence=False) is False
    assert len(calls) == 1
    assert calls[0][1] == "inspect"


def test_explicit_docker_timeout_still_overrides_environment(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_DOCKER_TIMEOUT", "120.5")
    observed = []

    def run(argv, **kwargs):
        observed.append(kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(docker.subprocess, "run", run)

    docker._docker("exec", "owned", "true", timeout=7.5)

    assert observed == [7.5]
