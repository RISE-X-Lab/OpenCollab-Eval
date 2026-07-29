from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def _entry() -> Any:
    module = importlib.import_module("opencollab_eval.commands.swe_eval_run")
    return importlib.reload(module)


def test_local_responses_relay_launches_as_raw_passthrough(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    module = _entry()
    written: list[dict[str, Any]] = []
    health = iter([False, True])
    monkeypatch.setattr(
        module,
        "_local_relay_healthy",
        lambda _url, _upstream, **_kwargs: next(health),
    )
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        module,
        "_launchctl",
        lambda *args, **kwargs: SimpleNamespace(returncode=1 if args[0] == "print" else 0),
    )
    monkeypatch.setattr(module, "_write_plist", lambda _path, payload: written.append(payload))
    monkeypatch.setattr(module.shutil, "copy2", lambda _source, _target: None)

    result = module._ensure_local_proxy_agent(
        output_dir=tmp_path,
        remaining=[
            "--local-proxy-base-url",
            "http://127.0.0.1:8879",
            "--proxy-env-file",
            "/private/tmp/relay.env",
        ],
        upstream_base_url="https://api.example.invalid/v1",
        relay_mode="responses-pass-through",
        compact_tool_schemas=False,
        max_upstream_request_bytes=0,
    )

    assert result["status"] == "started"
    program = written[0]["ProgramArguments"]
    assert "--chat-to-responses" not in program
    assert "--aggregate-chat-stream" not in program
    assert "--compact-tool-schemas" not in program
    assert "--max-upstream-request-bytes" not in program


def test_foreground_entry_binds_responses_relay_configuration(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    module = _entry()
    calls = []
    monkeypatch.setattr(module, "_ensure_proxy_agent", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(module, "_run_parallel_runner", lambda *_args: 0)

    assert module.main(
        [
            "--indices",
            "1",
            "--solver",
            "g11",
            "--run-id",
            "responses",
            "--output-dir",
            str(tmp_path),
            "--proxy-upstream-base-url",
            "https://api.example.invalid/v1",
            "--proxy-mode",
            "responses-pass-through",
            "--host",
            "host",
        ]
    ) == 0

    assert calls == [
        {
            "output_dir": tmp_path,
            "remaining": ["--host", "host"],
            "upstream_base_url": "https://api.example.invalid/v1",
            "relay_mode": "responses-pass-through",
            "compact_tool_schemas": False,
            "max_upstream_request_bytes": 0,
            "allow_insecure_upstream": False,
        }
    ]


def test_native_responses_mode_uses_raw_passthrough() -> None:
    module = _entry()

    assert module._relay_mode_flags(
        "responses-pass-through",
        compact_tool_schemas=False,
        max_upstream_request_bytes=0,
    ) == []


def test_insecure_upstream_requires_explicit_relay_flag() -> None:
    module = _entry()

    assert module._relay_mode_flags(
        "responses-pass-through",
        compact_tool_schemas=False,
        max_upstream_request_bytes=0,
        allow_insecure_upstream=True,
    ) == ["--allow-insecure-upstream"]


def test_detach_propagates_explicit_insecure_upstream(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    module = _entry()
    proxy_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(module, "_ensure_proxy_agent", lambda **kwargs: proxy_calls.append(kwargs) or {})
    monkeypatch.setattr(
        module,
        "_launchctl",
        lambda *arguments, **_kwargs: SimpleNamespace(
            returncode=1 if arguments[0] == "print" else 0,
            stdout="",
            stderr="",
        ),
    )
    monkeypatch.setattr(module, "_write_plist", lambda *_args: None)
    monkeypatch.setattr(module.shutil, "copy2", lambda *_args: None)

    assert module.main(
        [
            "--indices",
            "1",
            "--solver",
            "g11",
            "--output-dir",
            str(tmp_path),
            "--detach",
            "--host",
            "jinan",
            "--proxy-upstream-base-url",
            "http://api.example.invalid/v1",
            "--proxy-allow-insecure-upstream",
        ]
    ) == 0

    assert proxy_calls[0]["allow_insecure_upstream"] is True
