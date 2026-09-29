"""Effective model identity and retry-family behavior through public batch commands."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from opencollab import OpenCollab

from opencollab_eval.commands import batch as batch_cli
from opencollab_eval.experiment import batch_remote
from opencollab_eval.experiment.batch_model_probe import probe_script
from opencollab_eval.experiment.batch_spec import load_host
from tests.experiment.batch_support import FakeRemote, _facts
from tests.experiment.batch_support import experiment as experiment
from tests.experiment.batch_support import oc_repo as oc_repo
from tests.experiment.test_batch_replacement import _metrics, _plan, _replacement_spec, _report, _retry_spec, _run


@pytest.fixture(autouse=True)
def _isolated_model_environment(monkeypatch) -> None:
    provider_keys = {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DASHSCOPE_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_BASE_URL",
    }
    proxy_keys = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"}
    for key in list(os.environ):
        if (
            key.startswith("OPENCOLLAB_") and key != "OPENCOLLAB_EVAL_SOURCE_ROOT"
        ) or key in provider_keys | proxy_keys:
            monkeypatch.delenv(key)


def _local_host(experiment: dict):
    host = load_host(Path(experiment["dir"]) / "hosts" / "h.yaml")
    repo = Path(experiment["repo"])
    host = dataclasses.replace(
        host,
        workdir=str(repo.parent),
        python=sys.executable,
        proxy=None,
    )
    return SimpleNamespace(**vars(host), pythonpath=os.environ.get("PYTHONPATH", ""))


def _probe(experiment: dict, overrides: dict[str, str], *, endpoint: bool = False) -> dict[str, str]:
    host = _local_host(experiment)
    script = probe_script(host, "configs/.env", overrides, endpoint=endpoint)
    output = subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True, check=True).stdout
    return (
        batch_remote.facts_to_record(batch_remote.parse_facts(output))
        if not endpoint
        else {part[0]: part[1] for part in batch_remote.parse_facts(output) if len(part) > 1}
    )


def _public_config(experiment: dict):
    return OpenCollab(Path(experiment["repo"]).parent).configuration


class _LaunchRemote(FakeRemote):
    def run(self, script: str, timeout: float = 0) -> str:
        if "DECOY_HIT" in script:
            return super().run(script, timeout)
        if "create_model_client" in script:
            self.scripts.append(script)
            return "ENDPOINT\t200\n"
        return super().run(script, timeout)


def test_spec_overrides_match_runtime_config_probe_and_launch_record(experiment: dict, monkeypatch) -> None:
    repo = Path(experiment["repo"])
    envfile = repo / "configs" / ".env"
    envfile.write_text(
        "OPENCOLLAB_MODEL=file-model\nOPENCOLLAB_PROVIDER=openai\n"
        "OPENCOLLAB_BASE_URL=https://file.example/v1\nOPENCOLLAB_API_KEY=fake-file-key\n"
    )
    overrides = {
        "OPENCOLLAB_MODEL": "  override-model  ",
        "OPENCOLLAB_PROVIDER": "  ANTHROPIC  ",
        "ANTHROPIC_BASE_URL": "  https://provider.example/v1  ",
    }
    spec_path = Path(experiment["spec"])
    spec_path.write_text(
        spec_path.read_text().replace(
            'OPENCOLLAB_LLM_STREAM_CHAT: "true"',
            'OPENCOLLAB_LLM_STREAM_CHAT: "true"\n  OPENCOLLAB_MODEL: "  override-model  "\n'
            '  OPENCOLLAB_PROVIDER: "  ANTHROPIC  "\n  ANTHROPIC_BASE_URL: "  https://provider.example/v1  "',
        )
    )
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("OPENCOLLAB_CONFIG_FILE", str(envfile))
    actual = _public_config(experiment)
    facts = _probe(experiment, overrides)
    expected_url_sha = hashlib.sha256(b"https://provider.example/v1").hexdigest()
    assert (actual["model"], actual["provider"], actual["base_url_sha256"]) == (
        "override-model",
        "anthropic",
        expected_url_sha,
    )
    assert (facts["model"], facts["provider"], facts["base_url_sha256"]) == (
        actual["model"],
        actual["provider"],
        actual["base_url_sha256"],
    )

    original = _facts(experiment)
    for key, value in (
        ("MODEL", facts["model"]),
        ("PROVIDER", facts["provider"]),
        ("BASE_URL_SHA", facts["base_url_sha256"]),
    ):
        old = batch_remote.fact(batch_remote.parse_facts(original), key)
        original = original.replace(f"{key}\t{old}", f"{key}\t{value}")
    remote = _LaunchRemote(original)
    assert (
        batch_cli.main(
            ["--experiment-dir", str(experiment["dir"]), "launch", str(spec_path)],
            remote_factory=lambda host: remote,
        )
        == 0
    )
    record = json.loads((Path(experiment["dir"]).parent / "batches" / "t1.launch" / "batch.json").read_text())
    assert record["launches"][0]["model_identity"] == {
        "model": actual["model"],
        "provider": actual["provider"],
        "base_url_sha256": actual["base_url_sha256"],
    }
    assert any("OPENCOLLAB_MODEL=" in script and "override-model" in script for script in remote.scripts)


@pytest.mark.parametrize(
    ("provider", "protocol", "path", "body"),
    [
        (
            "openai",
            "chat_completions",
            "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "ok"}], "max_tokens": 1},
        ),
        ("openai", "responses", "/v1/responses", {"max_output_tokens": 1}),
        (
            "anthropic",
            "chat_completions",
            "/v1/messages",
            {"messages": [{"role": "user", "content": "ok"}], "max_tokens": 1},
        ),
    ],
)
def test_config_file_override_and_endpoint_probe_use_effective_values(
    experiment: dict, monkeypatch, provider: str, protocol: str, path: str, body: dict
) -> None:
    repo = Path(experiment["repo"])
    (repo / "configs" / ".env").write_text(
        "OPENCOLLAB_MODEL=file-model\nOPENCOLLAB_BASE_URL=https://wrong.example/v1\nOPENCOLLAB_API_KEY=old-key\n"
    )
    received: list[tuple[str, dict, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            key = self.headers.get("Authorization") or self.headers.get("x-api-key")
            received.append((self.path, body, key))
            if protocol == "responses":
                message = {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                }
                response = {
                    "id": "resp_test",
                    "object": "response",
                    "created_at": 1,
                    "status": "completed",
                    "model": "request-model",
                    "output": [message],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
                events = [
                    {"type": "response.created", "sequence_number": 0, "response": response},
                    {"type": "response.output_item.done", "sequence_number": 1, "output_index": 0, "item": message},
                    {"type": "response.completed", "sequence_number": 2, "response": response},
                ]
                encoded = b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events) + b"data: [DONE]\n\n"
                content_type = "text/event-stream"
            elif provider == "anthropic":
                response = {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "model": "request-model",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            else:
                response = {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "request-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            if protocol != "responses":
                encoded = json.dumps(response).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        other = repo / "configs" / "other.env"
        other.write_text(
            "OPENCOLLAB_MODEL=other-file-model\nOPENCOLLAB_PROVIDER=openai\n"
            "OPENCOLLAB_BASE_URL=https://other.example/v1\nOPENCOLLAB_API_KEY=other-key\n"
        )
        overrides = {
            "OPENCOLLAB_CONFIG_FILE": str(other),
            "OPENCOLLAB_MODEL": "request-model",
            "OPENCOLLAB_BASE_URL": (
                f"http://127.0.0.1:{server.server_port}" + ("" if provider == "anthropic" else "/v1")
            ),
            "OPENCOLLAB_API_KEY": "request-key",
            "OPENCOLLAB_PROVIDER": provider,
            "OPENCOLLAB_WIRE_PROTOCOL": protocol,
        }
        for key, value in overrides.items():
            monkeypatch.setenv(key, value)
        actual = _public_config(experiment)
        assert (actual["model"], actual["provider"], actual["wire_protocol"], actual["base_url_sha256"]) == (
            "request-model",
            provider,
            protocol,
            hashlib.sha256(overrides["OPENCOLLAB_BASE_URL"].encode()).hexdigest(),
        )
        facts = _probe(experiment, overrides)
        assert (facts["model"], facts["provider"], facts["base_url_sha256"]) == (
            actual["model"],
            actual["provider"],
            actual["base_url_sha256"],
        )
        endpoint = _probe(experiment, overrides, endpoint=True)
        assert endpoint["ENDPOINT"] == "200", endpoint
        assert len(received) == 1
        assert received[0][0] == path
        assert received[0][2] == ("request-key" if provider == "anthropic" else "Bearer request-key")
        assert received[0][1]["model"] == "request-model"
        if protocol == "responses":
            assert received[0][1]["input"]
        for key, value in body.items():
            assert received[0][1][key] == value
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_inherited_environment_matches_runtime_config(experiment: dict, monkeypatch) -> None:
    envfile = Path(experiment["repo"]) / "configs" / ".env"
    envfile.write_text(
        "OPENCOLLAB_MODEL=file-model\nOPENCOLLAB_PROVIDER=openai\nOPENCOLLAB_BASE_URL=https://file.example/v1\n"
    )
    monkeypatch.setenv("OPENCOLLAB_MODEL", "inherited-model")
    monkeypatch.setenv("OPENCOLLAB_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://inherited.example/v1")
    monkeypatch.setenv("OPENCOLLAB_CONFIG_FILE", str(envfile))
    actual = _public_config(experiment)
    facts = _probe(experiment, {})
    assert (actual["model"], actual["provider"], actual["base_url_sha256"]) == (
        "inherited-model",
        "anthropic",
        hashlib.sha256(b"https://inherited.example/v1").hexdigest(),
    )
    assert (facts["model"], facts["provider"], facts["base_url_sha256"]) == (
        actual["model"],
        actual["provider"],
        actual["base_url_sha256"],
    )


def test_quoted_model_file_matches_normalized_runtime_config(experiment: dict, monkeypatch) -> None:
    envfile = Path(experiment["repo"]) / "configs" / ".env"
    envfile.write_text(
        'OPENCOLLAB_MODEL="  file-model  "\n'
        "OPENCOLLAB_PROVIDER='  OPENAI  '\n"
        'OPENCOLLAB_BASE_URL="  https://quoted.example/v1  "\n'
    )
    monkeypatch.setenv("OPENCOLLAB_CONFIG_FILE", str(envfile))
    actual = _public_config(experiment)
    facts = _probe(experiment, {})
    assert (actual["model"], actual["provider"], actual["base_url_sha256"]) == (
        "file-model",
        "openai",
        hashlib.sha256(b"https://quoted.example/v1").hexdigest(),
    )
    assert (facts["model"], facts["provider"], facts["base_url_sha256"]) == (
        actual["model"],
        actual["provider"],
        actual["base_url_sha256"],
    )


def test_invalid_effective_config_cannot_be_recorded_as_a_model(experiment: dict, monkeypatch) -> None:
    envfile = Path(experiment["repo"]) / "configs" / ".env"
    envfile.write_text("OPENCOLLAB_MODEL=file-model\n")
    monkeypatch.setenv("OPENCOLLAB_CONFIG_FILE", str(envfile))
    monkeypatch.setenv("OPENCOLLAB_MODEL", "  ")
    host = _local_host(experiment)
    script = probe_script(host, "configs/.env", {"OPENCOLLAB_MODEL": "  "}, endpoint=False)
    output = subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True, check=True).stdout
    facts = batch_remote.parse_facts(output)
    assert batch_remote.fact(facts, "MODEL_ENV") == "invalid"
    assert batch_remote.fact(facts, "MODEL_CONFIG_ERROR") == "ValidationError"
    assert batch_remote.fact(facts, "MODEL") == ""


@pytest.mark.parametrize("status", [429, 503])
def test_endpoint_probe_does_not_retry_provider_errors(experiment: dict, monkeypatch, status: int) -> None:
    envfile = Path(experiment["repo"]) / "configs" / ".env"
    envfile.write_text("OPENCOLLAB_MODEL=probe-model\nOPENCOLLAB_PROVIDER=openai\nOPENCOLLAB_API_KEY=fake-key\n")
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            response = b'{"error":{"message":"transient","type":"server_error"}}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        overrides = {"OPENCOLLAB_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1"}
        monkeypatch.setenv("OPENCOLLAB_CONFIG_FILE", str(envfile))
        monkeypatch.setenv("OPENCOLLAB_BASE_URL", overrides["OPENCOLLAB_BASE_URL"])
        result = _probe(experiment, overrides, endpoint=True)
        assert result["ENDPOINT"] == str(status)
        assert requests == ["/v1/chat/completions"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    ("old", "changed"),
    [
        ("MODEL\tdeepseek-v4-flash", "MODEL\tmodel-B"),
        ("PROVIDER\topenai", "PROVIDER\tother-provider"),
        ("BASE_URL_SHA\tdeadbeef", "BASE_URL_SHA\tother-address"),
    ],
)
def test_paid_retry_family_rejects_drift_and_accepts_same_model(
    experiment: dict, tmp_path: Path, old: str, changed: str
) -> None:
    base = Path(experiment["spec"])
    args = ["--experiment-dir", str(experiment["dir"]), "launch"]
    facts_a = _facts(experiment)
    assert batch_cli.main([*args, str(base)], remote_factory=lambda host: _LaunchRemote(facts_a)) == 0
    first = _retry_spec(experiment, "t1r1", "t1", "rows: {start: 2, stop: 2}", "tiny")
    assert _plan(experiment, first) == 0
    facts_b = facts_a.replace(old, changed)
    assert batch_cli.main([*args, str(first)], remote_factory=lambda host: _LaunchRemote(facts_b)) == 2
    assert batch_cli.main([*args, str(first)], remote_factory=lambda host: _LaunchRemote(facts_a)) == 0
    second = _retry_spec(experiment, "t1r2", "t1r1", "rows: {start: 2, stop: 2}", "tiny")
    assert _plan(experiment, second) == 0
    assert batch_cli.main([*args, str(second)], remote_factory=lambda host: _LaunchRemote(facts_b)) == 2
    assert batch_cli.main([*args, str(second)], remote_factory=lambda host: _LaunchRemote(facts_a)) == 0

    root = Path(experiment["dir"]).parent / "batches"
    _metrics(root / "t1", [_run("a__a-1", tokens=10), _run("b__b-2", "failed", 1, "APIError")])
    _metrics(root / "t1r1", [_run("b__b-2", "failed", 2, "APIError")])
    _metrics(root / "t1r2", [_run("b__b-2", "completed", 3)])
    report = tmp_path / "report.json"
    assert _report(experiment, base, report) == 0
    doc = json.loads(report.read_text())
    assert next(row["source_batch"] for row in doc["runs"] if row["instance_id"] == "b__b-2") == "t1r2"
    assert doc["summary"]["tokens_all_attempts"] == 16


def test_unpaid_retry_can_choose_model_before_family_first_launch(experiment: dict) -> None:
    base = Path(experiment["spec"])
    retry = _retry_spec(experiment, "t1r1", "t1", "rows: {start: 2, stop: 2}", "tiny")
    assert _plan(experiment, base) == 0
    assert _plan(experiment, retry) == 0
    args = ["--experiment-dir", str(experiment["dir"])]
    facts_a = _facts(experiment)
    facts_b = facts_a.replace("MODEL\tdeepseek-v4-flash", "MODEL\tmodel-B")
    assert batch_cli.main([*args, "preflight", str(retry)], remote_factory=lambda h: _LaunchRemote(facts_a)) == 0
    assert batch_cli.main([*args, "preflight", str(retry)], remote_factory=lambda h: _LaunchRemote(facts_b)) == 0
    assert batch_cli.main([*args, "launch", str(retry)], remote_factory=lambda h: _LaunchRemote(facts_b)) == 0
    assert batch_cli.main([*args, "launch", str(base)], remote_factory=lambda h: _LaunchRemote(facts_a)) == 2
    assert batch_cli.main([*args, "launch", str(base)], remote_factory=lambda h: _LaunchRemote(facts_b)) == 0


def test_report_rejects_historical_mixed_paid_retry_models(experiment: dict, tmp_path: Path, capsys) -> None:
    base = Path(experiment["spec"])
    assert _plan(experiment, base) == 0
    first = _retry_spec(experiment, "t1r1", "t1", "rows: {start: 2, stop: 2}", "tiny")
    assert _plan(experiment, first) == 0
    second = _retry_spec(experiment, "t1r2", "t1r1", "rows: {start: 2, stop: 2}", "tiny")
    assert _plan(experiment, second) == 0
    root = Path(experiment["dir"]).parent / "batches"
    for name, model in (("t1", "model-A"), ("t1r1", "model-B"), ("t1r2", "model-C")):
        path = root / f"{name}.launch" / "batch.json"
        record = json.loads(path.read_text())
        record["launches"] = [
            {
                "at": "2026-09-29T00:00:00+00:00",
                "model_identity": {"model": model, "provider": "openai", "base_url_sha256": "deadbeef"},
            }
        ]
        path.write_text(json.dumps(record))
    _metrics(root / "t1", [_run("a__a-1", tokens=10), _run("b__b-2", "failed", 1, "APIError")])
    _metrics(root / "t1r1", [_run("b__b-2", "failed", 2, "APIError")])
    _metrics(root / "t1r2", [_run("b__b-2", "completed", 3)])
    report = tmp_path / "report.json"
    assert _report(experiment, base, report) == 2
    assert "recorded paid model identity" in capsys.readouterr().err
    assert not report.exists()

    for name in ("t1r1", "t1r2"):
        path = root / f"{name}.launch" / "batch.json"
        record = json.loads(path.read_text())
        record["launches"][0]["model_identity"]["model"] = "model-A"
        path.write_text(json.dumps(record))
    assert _report(experiment, base, report) == 0
    doc = json.loads(report.read_text())
    assert next(row["source_batch"] for row in doc["runs"] if row["instance_id"] == "b__b-2") == "t1r2"
    assert doc["summary"]["tokens_all_attempts"] == 16


def test_replacement_retry_merges_when_parent_metrics_are_absent(experiment: dict, tmp_path: Path) -> None:
    base = Path(experiment["spec"])
    assert _plan(experiment, base) == 0
    replacement = _replacement_spec(experiment, "t1x", "rows: {start: 3, stop: 3}", "b__b-2")
    assert _plan(experiment, replacement) == 0
    retry = _retry_spec(experiment, "t1xr", "t1x", "rows: {start: 3, stop: 3}", "tiny")
    assert _plan(experiment, retry) == 0
    later = _retry_spec(experiment, "t1xr2", "t1xr", "rows: {start: 3, stop: 3}", "tiny")
    assert _plan(experiment, later) == 0
    root = Path(experiment["dir"]).parent / "batches"
    _metrics(root / "t1", [_run("a__a-1", tokens=10), _run("b__b-2", tokens=20)])
    _metrics(root / "t1xr", [_run("c__c-3", "failed", 5, "APIError")])
    _metrics(root / "t1xr2", [_run("c__c-3", tokens=30)])
    report = tmp_path / "report.json"
    assert _report(experiment, base, report) == 0
    doc = json.loads(report.read_text())
    assert [row["instance_id"] for row in doc["runs"]] == ["a__a-1", "c__c-3"]
    assert doc["runs"][1]["source_batch"] == "t1xr2"
    assert [row["instance_id"] for row in doc["excluded"]] == ["b__b-2"]
    assert doc["summary"]["tokens_total"] == 40
    assert doc["summary"]["tokens_all_attempts"] == 45
