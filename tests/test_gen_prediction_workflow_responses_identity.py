"""Responses runtime identity evidence for workflow predictions."""

from __future__ import annotations

import json

import pytest
from gen_prediction_workflow_support import gpw


def test_responses_trajectory_binds_verified_provider_model(tmp_path):
    trace = tmp_path / "trajectory.jsonl"
    trace.write_text(
        json.dumps(
            {
                "type": "llm_call",
                "payload": {
                    "wire_protocol": "responses",
                    "model": "gpt-requested",
                    "provider_model": "gpt-requested",
                    "reasoning_effort_policy": "configured",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    models, digest = gpw._verified_provider_models(
        str(trace),
        artifact_root=tmp_path,
        expected_model="gpt-requested",
        expected_reasoning_effort=None,
        wire_protocol="responses",
    )
    assert models == ["gpt-requested"]
    assert len(digest) == 64

    record = json.loads(trace.read_text(encoding="utf-8"))
    record["payload"]["provider_model"] = "gpt-other"
    trace.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="provider model mismatch"):
        gpw._verified_provider_models(
            str(trace),
            artifact_root=tmp_path,
            expected_model="gpt-requested",
            expected_reasoning_effort=None,
            wire_protocol="responses",
        )


@pytest.mark.parametrize(
    "payload,match",
    [
        (
            {
                "wire_protocol": "chat_completions",
                "provider_model": "gpt-requested",
                "reasoning_effort_policy": "configured",
            },
            "mixed wire protocol",
        ),
        (
            {
                "provider_model": "gpt-requested",
                "reasoning_effort_policy": "configured",
            },
            "mixed wire protocol",
        ),
        (
            {"wire_protocol": "responses", "reasoning_effort_policy": "configured"},
            "provider model mismatch",
        ),
        (
            {
                "wire_protocol": "responses",
                "provider_model": "gpt-requested",
                "reasoning_effort": "high",
                "reasoning_effort_policy": "configured",
            },
            "reasoning effort mismatch",
        ),
    ],
)
def test_responses_trajectory_rejects_incomplete_identity(tmp_path, payload, match):
    trace = tmp_path / "trajectory.jsonl"
    trace.write_text(
        json.dumps({"type": "llm_call", "payload": payload}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match=match):
        gpw._verified_provider_models(
            str(trace),
            artifact_root=tmp_path,
            expected_model="gpt-requested",
            expected_reasoning_effort="medium",
            wire_protocol="responses",
        )


def test_responses_trajectory_rejects_external_and_symlinked_files(tmp_path):
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    external = tmp_path / "external.jsonl"
    external.write_text("{}\n", encoding="utf-8")
    link = artifact_root / "trajectory.jsonl"
    link.symlink_to(external)

    for path in (external, link):
        with pytest.raises(RuntimeError, match="outside|cannot be read"):
            gpw._verified_provider_models(
                str(path),
                artifact_root=artifact_root,
                expected_model="gpt-requested",
                expected_reasoning_effort=None,
                wire_protocol="responses",
            )


def test_responses_trajectory_accepts_role_level_reasoning_suppression(tmp_path):
    trace = tmp_path / "trajectory.jsonl"
    calls = [
        {
            "type": "llm_call",
            "payload": {
                "wire_protocol": "responses",
                "provider_model": "deepseek-v4-flash",
                "reasoning_effort": "max",
                "reasoning_effort_policy": "configured",
            },
        },
        {
            "type": "llm_call",
            "payload": {
                "wire_protocol": "responses",
                "provider_model": "deepseek-v4-flash",
                "reasoning_effort_policy": "suppressed",
            },
        },
    ]
    trace.write_text("".join(json.dumps(call) + "\n" for call in calls), encoding="utf-8")

    models, digest = gpw._verified_provider_models(
        str(trace),
        artifact_root=tmp_path,
        expected_model="deepseek-v4-flash",
        expected_reasoning_effort="max",
        wire_protocol="responses",
    )

    assert models == ["deepseek-v4-flash"]
    assert len(digest) == 64


def test_responses_trajectory_rejects_missing_reasoning_policy(tmp_path):
    trace = tmp_path / "trajectory.jsonl"
    trace.write_text(
        json.dumps(
            {
                "type": "llm_call",
                "payload": {
                    "wire_protocol": "responses",
                    "provider_model": "deepseek-v4-flash",
                    "reasoning_effort": "max",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="reasoning effort policy"):
        gpw._verified_provider_models(
            str(trace),
            artifact_root=tmp_path,
            expected_model="deepseek-v4-flash",
            expected_reasoning_effort="max",
            wire_protocol="responses",
        )
