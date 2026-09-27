"""Compatibility checks owned by the extracted evaluation package."""

from __future__ import annotations

import importlib

from opencollab_eval.commands import swe_v1_prolite_runner


def test_split_command_facade_retains_legacy_public_binding() -> None:
    module = importlib.import_module("opencollab_eval.commands.swe_v1_prolite_runner")
    assert "base64" in vars(module)


def test_prolite_runner_retains_legacy_public_launcher_names() -> None:
    functions = {
        "run_checked", "load_shell_env", "token_from_values",
        "token_from_env_file", "proxy_env_file_from_ps", "get_proxy_token",
        "url_with_healthz", "local_http_ok", "remote_http_ok",
        "loopback_port", "loopback_url_with_port",
        "remote_forward_port_conflict", "stop_remote_proxy_tunnel",
        "cleanup_remote_proxy_tunnels", "start_remote_proxy_tunnel",
        "ensure_remote_proxy", "sync_runtime", "configure_run_paths",
        "terminate_remote_run", "terminate_local_process_group",
        "run_remote", "write_local_report", "main",
    }
    constants = {
        "DEFAULT_HOST", "DEFAULT_REMOTE_ROOT", "DEFAULT_BASE_RUN_DIR_PREFIX",
        "DEFAULT_MODEL_NAME", "DEFAULT_REPORT_JSON", "DEFAULT_REPORT_MD",
        "DEFAULT_PROXY_ENV_FILE", "DEFAULT_LOCAL_PROXY_BASE_URL",
        "REMOTE_HEALTH_SSH_TIMEOUT_FLOOR", "REMOTE_PROXY_TUNNELS",
        "REMOTE_RUNNER",
    }
    for name in functions | constants:
        assert hasattr(swe_v1_prolite_runner, name), name
    assert swe_v1_prolite_runner.loopback_port("http://127.0.0.1", default=18788) == 18788
    assert "swe_v1_remote_runner" in swe_v1_prolite_runner.REMOTE_RUNNER
    compile(swe_v1_prolite_runner.REMOTE_RUNNER, "<REMOTE_RUNNER>", "exec")
