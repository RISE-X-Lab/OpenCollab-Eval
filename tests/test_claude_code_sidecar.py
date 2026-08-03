from __future__ import annotations

import json
from pathlib import Path

import pytest
from claude_code_test_support import (
    RUNTIME_IMAGE,
    TASK_IMAGE_ID,
    build_existing_sidecar,
    build_sidecar_fixture,
)

from opencollab_eval.generation import claude_code_sidecar as ccs
from opencollab_eval.generation import external_solver_usage as esu


def test_claude_sidecar_normalizes_cached_input_and_binds_runtime(tmp_path: Path) -> None:
    sidecar = build_sidecar_fixture(tmp_path)
    settings = json.loads((tmp_path / "claude.settings.json").read_text())

    assert sidecar["success"] is True
    assert sidecar["observed_models"] == ["glm-5.2"]
    assert sidecar["model_usage_models"] == ["glm-5.2"]
    assert sidecar["usage"] == {
        "raw_input_tokens": 100,
        "input_tokens": 420,
        "cache_read_tokens": 300,
        "cache_creation_tokens": 20,
        "output_tokens": 40,
        "total_tokens": 460,
    }
    assert sidecar["runtime_image"] == RUNTIME_IMAGE
    assert sidecar["task_image_id"] == TASK_IMAGE_ID
    assert sidecar["invocation_binding"]["solver_task_id"] == "solver-" + "1" * 32
    assert len(sidecar["executable"]["sha256"]) == 64
    assert len(sidecar["stream_sha256"]) == 64
    assert len(sidecar["settings_sha256"]) == 64
    assert {
        settings["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"],
        settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"],
        settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"],
    } == {"glm-5.2"}
    assert "sandbox" not in settings


def test_relay_upstream_rewrites_only_loopback_hosts() -> None:
    assert ccs.relay_upstream_url("http://127.0.0.1:18790/api/anthropic?x=1") == (
        "http://host.docker.internal:18790/api/anthropic?x=1"
    )
    with pytest.raises(ValueError, match="loopback"):
        ccs.relay_upstream_url("https://api.example.invalid/v1")
    assert ccs.relay_socket_path("http://127.0.0.1:18790") == (
        "/tmp/opencollab-llmproxy-18790.sock"
    )
    with pytest.raises(ValueError, match="loopback"):
        ccs.relay_socket_path("https://api.example.invalid/v1")


@pytest.mark.parametrize(
    ("options", "expected_error"),
    [
        ({"model": "claude-sonnet-4-6"}, "stream model identity"),
        ({"include_result": False}, "exactly one result"),
        ({"success": False}, "result is not successful"),
    ],
)
def test_claude_sidecar_rejects_invalid_result_evidence(
    tmp_path: Path, options: dict[str, object], expected_error: str
) -> None:
    sidecar = build_sidecar_fixture(tmp_path, **options)

    assert sidecar["success"] is False
    assert any(expected_error in error for error in sidecar["errors"])


def test_claude_sidecar_classifies_synthetic_api_failure_without_model_drift(
    tmp_path: Path,
) -> None:
    sidecar = build_sidecar_fixture(
        tmp_path,
        process_returncode=1,
        synthetic_api_error=True,
    )

    assert sidecar["success"] is False
    assert sidecar["observed_models"] == ["glm-5.2"]
    assert sidecar["model_usage_models"] == ["glm-5.2"]
    assert sidecar["synthetic_events"] == 1
    assert sidecar["transport_failure"] is True
    assert sidecar["evidence_valid"] is True
    assert sidecar["candidate_binding_complete"] is True
    assert sidecar["candidate_ready"] is False
    assert sidecar["solver_outcome"] == "transport_failure"
    assert "Claude stream reports an API transport failure" in sidecar["errors"]
    assert not any("model identity" in error for error in sidecar["errors"])
    assert "Claude Code exited with 1" in sidecar["errors"]


def test_incomplete_turn_preserves_a_bound_candidate(tmp_path: Path) -> None:
    sidecar = build_sidecar_fixture(tmp_path, stop_reason="tool_use")

    assert sidecar["success"] is False
    assert sidecar["evidence_valid"] is True
    assert sidecar["candidate_binding_complete"] is True
    assert sidecar["candidate_ready"] is False
    assert sidecar["solver_outcome"] == "incomplete_turn"
    assert sidecar["tool_calls"] == {
        "tool_use_count": 0,
        "tool_result_count": 0,
        "balanced": True,
    }


def test_unmatched_tool_call_marks_incomplete_outcome_without_blocking_candidate(
    tmp_path: Path,
) -> None:
    sidecar = build_sidecar_fixture(
        tmp_path,
        stop_reason="tool_use",
        tool_use_without_result=True,
    )

    assert sidecar["evidence_valid"] is True
    assert sidecar["candidate_binding_complete"] is True
    assert sidecar["solver_outcome"] == "incomplete_turn"
    assert sidecar["tool_calls"]["balanced"] is False
    assert "Claude tool calls are incomplete" in sidecar["outcome_errors"]


def test_missing_result_preserves_identity_bound_candidate(tmp_path: Path) -> None:
    sidecar = build_sidecar_fixture(tmp_path, include_result=False)

    assert sidecar["success"] is False
    assert sidecar["evidence_valid"] is True
    assert sidecar["candidate_binding_complete"] is True
    assert sidecar["solver_outcome"] == "incomplete_turn"
    assert "Claude stream must contain exactly one result record" in sidecar["outcome_errors"]
    assert "Claude modelUsage is missing" in sidecar["usage_errors"]


def test_unclassified_synthetic_message_preserves_candidate_identity(tmp_path: Path) -> None:
    stream = tmp_path / "claude.stream.jsonl"
    build_sidecar_fixture(tmp_path)
    rows = [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines()]
    rows.insert(
        -1,
        {
            "type": "assistant",
            "message": {
                "model": "<synthetic>",
                "content": [{"type": "text", "text": "unexpected local message"}],
            },
        },
    )
    stream.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    sidecar = build_existing_sidecar(tmp_path)

    assert sidecar["success"] is False
    assert sidecar["evidence_valid"] is True
    assert sidecar["candidate_binding_complete"] is True
    assert sidecar["synthetic_events"] == 1
    assert sidecar["transport_failure"] is False
    assert "Claude stream contains an unclassified synthetic event" in sidecar["outcome_errors"]


def test_trailing_partial_json_preserves_candidate_identity(tmp_path: Path) -> None:
    stream = tmp_path / "claude.stream.jsonl"
    build_sidecar_fixture(tmp_path, include_result=False)
    with stream.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant","message":')

    sidecar = build_existing_sidecar(tmp_path)

    assert sidecar["success"] is False
    assert sidecar["evidence_valid"] is True
    assert sidecar["candidate_binding_complete"] is True
    assert sidecar["solver_outcome"] == "incomplete_turn"
    assert sidecar["parse_warnings"] == [
        "Claude stream ends with partial JSON at line 3"
    ]


def test_malformed_json_between_valid_records_blocks_candidate_identity(
    tmp_path: Path,
) -> None:
    stream = tmp_path / "claude.stream.jsonl"
    build_sidecar_fixture(tmp_path)
    rows = stream.read_text(encoding="utf-8").splitlines()
    rows.insert(1, '{"type":"assistant","message":')
    stream.write_text("\n".join(rows) + "\n", encoding="utf-8")

    sidecar = build_existing_sidecar(tmp_path)

    assert sidecar["evidence_valid"] is False
    assert sidecar["candidate_binding_complete"] is False
    assert sidecar["candidate_ready"] is False
    assert "Claude stream line 2 is not JSON" in sidecar["evidence_errors"]


def test_real_assistant_model_drift_blocks_candidate_identity(tmp_path: Path) -> None:
    stream = tmp_path / "claude.stream.jsonl"
    build_sidecar_fixture(tmp_path)
    rows = [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines()]
    rows[1]["message"]["model"] = "unexpected-model"
    stream.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    sidecar = build_existing_sidecar(tmp_path)

    assert sidecar["observed_models"] == ["glm-5.2", "unexpected-model"]
    assert sidecar["model_usage_models"] == ["glm-5.2"]
    assert sidecar["evidence_valid"] is False
    assert sidecar["candidate_binding_complete"] is False
    assert sidecar["candidate_ready"] is False
    assert "Claude stream model identity does not equal glm-5.2" in sidecar["evidence_errors"]


@pytest.mark.parametrize("total_cost_usd", [float("nan"), float("inf"), float("-inf")])
def test_claude_sidecar_rejects_non_finite_reported_cost(
    tmp_path: Path,
    total_cost_usd: float,
) -> None:
    sidecar = build_sidecar_fixture(tmp_path, total_cost_usd=total_cost_usd)

    assert sidecar["success"] is False
    assert sidecar["cost_usd"] == 0.0
    assert "invalid total_cost_usd" in sidecar["errors"]


def test_failed_claude_result_preserves_usage_and_reported_cost(tmp_path: Path) -> None:
    sidecar = build_sidecar_fixture(tmp_path, success=False)
    (tmp_path / "external_solver.sidecar.json").write_text(
        json.dumps(sidecar), encoding="utf-8"
    )

    usage_evidence = esu._external_solver_usage_evidence(tmp_path)
    values = esu._external_solver_usage(usage_evidence)

    assert usage_evidence is not None
    assert usage_evidence["success"] is False
    assert values is not None
    assert values["input_tokens"] == 420
    assert values["claude_reported_cost_usd"] == 1.25
    payload = esu._append_usage_record(
        run_dir=tmp_path,
        instance_id="failed-instance",
        model="glm-5.2",
        usage_values=values,
        provider="claude-code",
        status="technical_failure",
    )
    record = json.loads((tmp_path / "api_usage.jsonl").read_text(encoding="utf-8"))
    assert record["status"] == "technical_failure"
    assert payload["cost_usd"] == 1.25
