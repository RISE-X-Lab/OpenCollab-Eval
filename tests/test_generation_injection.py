"""Generation uses the caller's workflow and deployment launcher."""

from types import SimpleNamespace

import pytest
from swe_v1_prolite_runner_test_support import _remote_config, _remote_namespace

from opencollab_eval.engine import swe_v1_remote_state


def test_generation_dispatches_injected_launcher_and_workflow(tmp_path, monkeypatch):
    launcher = str(tmp_path / "caller-launch.sh")
    namespace = _remote_namespace(
        tmp_path,
        workflow="caller-workflow",
        workflow_reference="caller.workflows:solve",
        workflow_label="selected-method",
        candidate_environment="lean",
        generation_launcher=launcher,
        run_id="owned-run",
        workflow_env={"OPENCOLLAB_EVAL_RUN_ID": "overridden"},
        workflow_env_keys=["OPENCOLLAB_EVAL_RUN_ID"],
    )
    captured = {}

    def spawn(command, **kwargs):
        captured.update(command=command, **kwargs)
        return SimpleNamespace(pid=123)

    namespace["ensure_image"] = lambda _image: {"ok": True, "image_id": "sha256:" + "9" * 64}
    namespace["generation_done"] = lambda *_args, **_kwargs: (False, None, None, "missing")
    namespace["write_start_state"] = lambda *_args: {}
    namespace["write_fifo_with_timeout"] = lambda *_args: {"ok": True}
    namespace["wait_generation"] = lambda *_args, **_kwargs: 1
    namespace["ensure_process_group_quiesced_after_wait"] = lambda _process: True
    monkeypatch.setattr(namespace["subprocess"], "Popen", spawn)

    outcome = namespace["generation_for_task_once"]({"instance_id": "task-1", "dockerhub_tag": "image"})

    assert outcome["status"] == "generation_failed"
    assert captured["command"][0] == launcher
    assert captured["env"]["OPENCOLLAB_EVAL_RUN_ID"] == "owned-run"
    assert captured["env"]["OPENCOLLAB_SWE_WORKFLOW_ENTRYPOINT"] == "caller.workflows:solve"
    assert captured["env"]["OPENCOLLAB_SWE_WORKFLOW"] == "selected-method"
    assert captured["env"]["OPENCOLLAB_SWE_CANDIDATE_ENVIRONMENT"] == "lean"
    assert namespace["ACTIVE_CHILD_PGIDS"] == set()
    assert namespace["ACTIVE_FIFO_PATHS"] == set()


def test_configured_workflow_environment_keys_keep_explicit_allowance(tmp_path):
    config = _remote_config(
        tmp_path,
        workflow_env={"OPENCOLLAB_CALLER_ROLE_BUDGET": "20"},
        workflow_env_keys=["OPENCOLLAB_CALLER_ROLE_BUDGET"],
    )
    swe_v1_remote_state.configure(config)
    assert swe_v1_remote_state.workflow_env == {"OPENCOLLAB_CALLER_ROLE_BUDGET": "20"}

    config.pop("workflow_env_keys")
    with pytest.raises(ValueError, match="unsupported workflow env"):
        swe_v1_remote_state.configure(config)


def test_direct_transport_accepts_a_configured_openai_endpoint(tmp_path):
    credential = tmp_path / "provider.env"
    credential.write_text("OPENAI_API_KEY=fixture-token\n", encoding="utf-8")
    credential.chmod(0o600)
    config = _remote_config(
        tmp_path,
        token="",
        remote_api_env_file=str(credential),
        remote_proxy_base_url="https://provider.example.invalid/v1",
        llm_transport="direct",
        llm_provider="openai",
        llm_model="caller-model",
    )
    swe_v1_remote_state.configure(config)

    assert swe_v1_remote_state.token == "fixture-token"
    assert swe_v1_remote_state.remote_proxy_base_url == "https://provider.example.invalid/v1"
    assert swe_v1_remote_state.llm_model == "caller-model"


def test_direct_transport_keeps_endpoint_validation(tmp_path):
    config = _remote_config(
        tmp_path,
        eval_only=True,
        token="",
        llm_transport="direct",
        llm_provider="openai",
        llm_model="caller-model",
        remote_proxy_base_url="invalid-endpoint",
    )
    with pytest.raises(ValueError, match="HTTP"):
        swe_v1_remote_state.configure(config)


@pytest.mark.parametrize("key", ["OPENCOLLAB_API_KEY", "OPENCOLLAB_PROXY_CLIENT_TOKEN", "BASH_ENV"])
def test_workflow_environment_extensions_keep_credentials_and_shell_startup_separate(tmp_path, key):
    config = _remote_config(tmp_path, workflow_env_keys=[key])
    with pytest.raises(ValueError, match="credential|OPENCOLLAB"):
        swe_v1_remote_state.configure(config)


def test_runner_namespaces_keep_their_own_injected_launcher(tmp_path):
    first_launcher = str(tmp_path / "first-launch.sh")
    second_launcher = str(tmp_path / "second-launch.sh")
    first = _remote_namespace(tmp_path / "first", generation_launcher=first_launcher)
    second = _remote_namespace(tmp_path / "second", generation_launcher=second_launcher)

    assert first["generation_launcher_for_task"]() == first_launcher
    assert second["generation_launcher_for_task"]() == second_launcher
