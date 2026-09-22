"""The single arm can seat Single2, the agent the s2dual cells seat in every role.

The s2dual family gives each of its three roles ``profile: single2``, so every
seat runs OpenCollab's Single2 agent -- its own system prompt, its six tools,
its history shaper and safety wrapper -- with the role's card appended. The
single arm ran something else: ``client.agent`` with this repository's
``AGENT_PROMPT`` and working bundle. A team-minus-single difference between the
two was therefore the organization *and* the seat, and ITT/PP/CACE on the
family could not be computed. ``profile: single2`` on a single-arm spec runs
the same Single2 agent alone: the profile supplies the prompt and the tools,
and the task text is the one the team arm's Adopter receives.

The field is part of the batch identity only when set, so every spec written
before it keeps its digest, and a retry of a Single2 batch must carry it too.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from gen_prediction_single_agent_support import (
    RecordingRuntime,
    _agent_config,
    _reserve_empty_artifact_dir,
    _runtime_result,
)

from opencollab_eval.experiment.batch_spec import (
    HostConfig,
    SpecError,
    driver_argv,
    load_spec,
    spec_digest,
    spec_identity,
)

gp = pytest.importorskip("opencollab_eval.generation.gen_prediction")

SHA = "a" * 40
SPEC = textwrap.dedent(
    f"""\
    name: s1
    host: h
    arm: single
    suite: tiny
    rows: {{start: 1, stop: 3}}
    budget_per_seat: 2000000
    max_steps: 200
    timeout: 5400
    concurrency: 3
    model_env: configs/.env
    env:
      OPENCOLLAB_LLM_STREAM_CHAT: "true"
      OPENCOLLAB_REASONING_EFFORT: "max"
      OPENCOLLAB_WRITE_NUDGE_MODE: "off"
    pins:
      opencollab: {SHA}
      opencollab_eval: {SHA}
    """
)


def _spec(tmp_path: Path, text: str):
    path = tmp_path / "s.yaml"
    path.write_text(text, encoding="utf-8")
    return load_spec(path)


def _host() -> HostConfig:
    return HostConfig(
        name="h",
        ssh="h",
        workdir="/home/u",
        python="/home/u/venv/bin/python",
        opencollab_dir="OpenCollab",
        eval_dir="OpenCollab-Eval",
        proxy=None,
        docker_disk="/home",
        min_free_gb=10.0,
        local_batches_dir="/tmp/b",
        local_opencollab_dir="/tmp/oc",
        frame_content="/tmp/frame.jsonl",
    )


def test_a_single2_spec_carries_the_profile_into_its_identity(tmp_path):
    plain = _spec(tmp_path, SPEC)
    single2 = _spec(tmp_path, SPEC + "profile: single2\n")

    assert single2.profile == "single2"
    assert spec_identity(single2)["profile"] == "single2"
    # Same rows, same budget, same pins: still a different batch.
    assert spec_digest(single2) != spec_digest(plain)


def test_a_spec_without_a_profile_keeps_the_identity_it_always_had(tmp_path):
    plain = _spec(tmp_path, SPEC)

    assert plain.profile is None
    assert "profile" not in spec_identity(plain)


def test_a_profile_is_refused_on_an_arm_that_does_not_seat_one_agent(tmp_path):
    team = SPEC.replace("arm: single\n", "arm: team\ncell: s2dual-judge\n")
    with pytest.raises(SpecError, match="profile"):
        _spec(tmp_path, team + "profile: single2\n")


def test_an_unknown_profile_is_refused(tmp_path):
    with pytest.raises(SpecError, match="profile"):
        _spec(tmp_path, SPEC + "profile: single3\n")


def test_the_driver_passes_the_profile_to_every_single_arm_run(tmp_path):
    single2 = _spec(tmp_path, SPEC + "profile: single2\n")

    argv = driver_argv(single2, _host(), limit=3)

    # --pass-through swallows the rest of argv, so it has to come last and
    # --limit has to come before it.
    assert argv[-3:] == ["--pass-through", "--agent-profile", "single2"]
    assert argv.index("--limit") < argv.index("--pass-through")
    assert "--pass-through" not in driver_argv(_spec(tmp_path, SPEC), _host())


def test_run_agent_seats_the_profile_rather_than_this_repository_s_prompt(monkeypatch, tmp_path):
    artifact_dir = _reserve_empty_artifact_dir(monkeypatch, tmp_path)
    runtime = RecordingRuntime(_runtime_result())

    asyncio.run(
        gp.run_agent(
            "task",
            "cid",
            _agent_config(),
            200,
            2_000_000,
            5400.0,
            artifact_root=tmp_path,
            runtime=runtime,
            agent_profile="single2",
        )
    )

    request = runtime.requests[0]
    assert request.prompt == "task"
    assert request.profile == "single2"
    # The profile's own system prompt and tools, not AGENT_PROMPT and the
    # working bundle: passing either would override the profile's.
    assert not hasattr(request, "system_prompt")
    assert not hasattr(request, "tools")
    assert request.name == "swe_agent"
    assert (request.budget, request.max_steps, request.timeout) == (2_000_000, 200, 5400.0)
    assert request.artifacts == artifact_dir
    assert request.trace is True


def test_the_single_arm_cli_takes_the_profile():
    result = subprocess.run(
        [sys.executable, "-m", "opencollab_eval.generation.gen_prediction", "--help"],
        text=True,
        capture_output=True,
        check=True,
    )

    # The flag and its one allowed value, as argparse lists them: a substring
    # test would also pass for any flag that merely starts with this name.
    assert re.search(r"^\s+--agent-profile \{single2\}", result.stdout, re.MULTILINE)
