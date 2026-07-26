"""Fast contract tests for the deterministic SWE end-to-end scenario."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from e2e.deterministic_swe_driver import (
    TARGET_TEST,
    _clean_environment,
    _start_fake_service,
    _stop_fake_service,
    validate_official_execution,
    validate_patch_identity,
    validate_runtime_identity,
    wait_for_service,
)
from e2e.evidence_publish import publish_production_evidence
from e2e.fake_openai_server import (
    EXPECTED_THINKING,
    FAKE_API_KEY,
    MODEL,
    PROVIDER_KEY_NAMES,
    SOURCE_PATH,
    validate_generation_request,
)
from opencollab_eval.patch_diff import patch_paths


def _request_payload() -> dict:
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": "fix calculator.add"}],
        "temperature": 1,
        "top_p": 0.95,
        "max_tokens": 32768,
        "thinking": EXPECTED_THINKING,
        "tools": [
            {"type": "function", "function": {"name": name, "parameters": {}}}
            for name in ("file_read", "file_write", "bash")
        ],
    }


def test_fake_model_rejects_wrong_model_and_thinking_identity():
    wrong_model = _request_payload()
    wrong_model["model"] = "kimi-k2.6"
    assert "model must equal 'kimi-for-coding'" in validate_generation_request(wrong_model)

    wrong_thinking = _request_payload()
    wrong_thinking["thinking"] = {"type": "enabled", "keep": "none"}
    assert "thinking must equal" in validate_generation_request(wrong_thinking)[0]


def test_fake_model_runs_as_a_real_local_http_service(tmp_path):
    process, base_url = _start_fake_service(tmp_path)
    try:
        model_request = urllib.request.Request(
            base_url + "/models",
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        with urllib.request.urlopen(model_request, timeout=5) as response:
            models = json.load(response)
        assert [item["id"] for item in models["data"]] == [MODEL]

        request = urllib.request.Request(
            base_url + "/chat/completions",
            data=json.dumps(_request_payload()).encode(),
            headers={
                "Authorization": f"Bearer {FAKE_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            completion = json.load(response)
        call = completion["choices"][0]["message"]["tool_calls"][0]
        assert completion["model"] == MODEL
        assert call["function"]["name"] == "file_read"
    finally:
        assert _stop_fake_service(process) is True


def test_provider_credentials_are_removed_from_child_environment(monkeypatch, tmp_path):
    for name in PROVIDER_KEY_NAMES:
        monkeypatch.setenv(name, "credential-canary")
    assert not set(PROVIDER_KEY_NAMES).intersection(_clean_environment())

    process, _base_url = _start_fake_service(
        tmp_path, forbidden_env_value="credential-canary"
    )
    try:
        events = [
            json.loads(line)
            for line in (tmp_path / "fake-model-trace.jsonl").read_text().splitlines()
        ]
        assert events[0]["provider_environment_clean"] is True
    finally:
        assert _stop_fake_service(process) is True


def test_patch_parser_preserves_space_and_literal_b_path():
    patch = (
        f"diff --git a/{SOURCE_PATH} b/{SOURCE_PATH}\n"
        f"--- a/{SOURCE_PATH}\n"
        f"+++ b/{SOURCE_PATH}\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    assert patch_paths(patch) == [SOURCE_PATH]


def test_candidate_patch_sha_mismatch_is_rejected():
    patch = "diff --git a/a.py b/a.py\n"
    metric = {
        "instance_id": "task-1",
        "model_name_or_path": MODEL,
        "record_id": "record-1",
        "patch_sha256": "0" * 64,
    }
    prediction = {**metric, "model_patch": patch, "workflow_metric": metric}

    with pytest.raises(RuntimeError, match="patch_sha256"):
        validate_patch_identity(prediction, metric, instance_id="task-1")


def test_production_evidence_paths_remain_portable_after_work_cleanup(tmp_path):
    artifact = tmp_path / "artifact"
    production = artifact / "work" / "production-run"
    generation_log = production / "task" / "generation.log"
    direct_report = production / "task" / "eval" / "report.json"
    command_log = production / "task" / "eval" / "command.log"
    for path, text in (
        (generation_log, "generated\n"),
        (direct_report, "{}\n"),
        (command_log, "tested\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    report = {
        "base_run_dir": str(production),
        "remote_runtime_repo": str(artifact / "work" / "remote-runtime"),
        "runtime_sync": {
            "remote_runtime_repo": str(artifact / "work" / "remote-runtime")
        },
        "markdown": f"base: {production}\nreport: {direct_report}\n",
        "rows": [
            {
                "generation": {"log": str(generation_log)},
                "eval": {
                    "report_path": str(direct_report),
                    "summary": {
                        "report_path": str(direct_report),
                        "command_log": str(command_log),
                    },
                },
            }
        ]
    }
    markdown = f"report: {direct_report}\n"

    published, published_markdown, index = publish_production_evidence(
        report, markdown, artifact_dir=artifact, production_run=production
    )

    assert published["rows"][0]["generation"]["log"] == (
        "production-evidence/generation_log.txt"
    )
    assert str(artifact / "work") not in json.dumps(published)
    assert str(artifact / "work") not in published_markdown
    assert set(index["files"]) == {
        "generation_log",
        "direct_eval_report",
        "direct_eval_command_log",
    }
    assert all((artifact / item["path"]).is_file() for item in index["files"].values())


def test_k27_context_window_identity_mismatch_is_rejected():
    metric = {
        "llm_model": MODEL,
        "llm_provider": "openai",
        "context_window": 131072,
        "temperature": 1.0,
        "top_p": 0.95,
        "max_output_tokens": 32768,
    }
    with pytest.raises(RuntimeError, match="context_window"):
        validate_runtime_identity(metric)


@pytest.mark.parametrize(
    ("status_map", "output", "found"),
    [
        ({}, "collected 0 items", True),
        ({TARGET_TEST: "PASSED"}, "collected 0 items", True),
        ({TARGET_TEST: "PASSED"}, "1 passed", False),
    ],
)
def test_official_zero_or_unproven_tests_are_rejected(status_map, output, found):
    with pytest.raises(RuntimeError, match="execution proof"):
        validate_official_execution(status_map, output, found=found)


def test_fake_model_early_exit_is_detected(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"])
    with pytest.raises(RuntimeError, match="exited before becoming ready"):
        wait_for_service(process, tmp_path / "never-ready", timeout=2)
    process.wait(timeout=5)


def test_process_watchdog_terminates_a_timed_out_process_group(tmp_path):
    pid_path = tmp_path / "child.pid"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "e2e.process_watchdog",
            "--timeout",
            "0.1",
            "--grace",
            "0.1",
            "--",
            sys.executable,
            "-c",
            (
                "import os,time; from pathlib import Path; "
                f"Path({str(pid_path)!r}).write_text(str(os.getpid())); time.sleep(30)"
            ),
        ],
        cwd=Path(__file__).parents[1],
        env={key: value for key, value in os.environ.items() if key not in PROVIDER_KEY_NAMES},
        timeout=5,
        check=False,
    )
    assert result.returncode == 124
    child_pid = int(pid_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
