from __future__ import annotations

import pytest
from gen_prediction_openhands_support import (
    install_fake_openhands_process as _install_fake_openhands_process,
)

from opencollab_eval.generation import claude_code_sidecar as ccs
from opencollab_eval.generation import gen_prediction_openhands as gpo


def _settings(**updates: str) -> list[str]:
    values = {
        "OPENCOLLAB_CLAUDE_EXPECTED_MODEL": "glm-5.2",
        "OPENCOLLAB_CLAUDE_EXPECTED_VERSION": "2.1.175",
        "OPENCOLLAB_CLAUDE_RUNTIME_IMAGE": "claude-runtime:2.1.175",
        "OPENCOLLAB_CLAUDE_RUNTIME_IMAGE_ID": "sha256:" + "a" * 64,
    }
    values.update(updates)
    return [f"{name}={value}" for name, value in values.items()]


@pytest.mark.parametrize(
    "settings",
    [
        [],
        _settings(OPENCOLLAB_CLAUDE_EXPECTED_MODEL="other-model"),
        _settings(OPENCOLLAB_CLAUDE_RUNTIME_IMAGE_ID="mutable-tag"),
    ],
)
def test_runtime_identity_rejects_missing_or_invalid_settings(settings: list[str]) -> None:
    with pytest.raises(ValueError, match="claude-code requires"):
        ccs.validate_runtime_workflow_settings(settings)


def test_external_solver_subprocess_receives_runtime_identity(tmp_path, monkeypatch) -> None:
    expected = dict(value.split("=", 1) for value in _settings())
    for name, value in expected.items():
        monkeypatch.setenv(name, value)
    captured = {}
    _install_fake_openhands_process(monkeypatch, stdout="done", captured=captured)
    gpo._run_openhands(
        command_template="solver {prompt_file}",
        container_id="container-123",
        instance={"instance_id": "acme__widget-1"},
        instance_file=tmp_path / "instance.json",
        prompt_file=tmp_path / "prompt.md",
        output_dir=tmp_path / "output",
        timeout=5,
    )
    assert {name: captured["env"][name] for name in expected} == expected
