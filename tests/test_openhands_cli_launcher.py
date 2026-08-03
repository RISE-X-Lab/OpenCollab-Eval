from __future__ import annotations

import os
import subprocess
from pathlib import Path

from package_test_support import resource_path

from opencollab_eval.generation import gen_prediction_openhands as gpo

SCRIPT = resource_path("run_openhands_cli.sh")
CONFIGURATION_KEYS = (
    "OPENCOLLAB_REMOTE_REPO",
)


def _clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in CONFIGURATION_KEYS:
        environment.pop(key, None)
    return environment


def test_prompt_requires_all_repository_work_to_use_the_existing_container() -> None:
    prompt = gpo._prompt(
        {
            "repo": "acme/widget",
            "problem_statement": "Fix the widget.",
            "hints_text": "Inspect parser.py.",
        },
        container_id="container-123",
    )

    assert "docker exec" not in prompt
    assert gpo.gp.DOCKER_WORKDIR in prompt
    assert "isolated, offline workspace" in prompt
    assert "git status --short" in prompt


def test_openhands_launcher_fails_fast_without_runtime_configuration() -> None:
    completed = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        env=_clean_environment(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "OPENCOLLAB_REMOTE_REPO" in completed.stderr


def test_openhands_launcher_uses_the_configured_python_environment(tmp_path: Path) -> None:
    remote_repo = tmp_path / "repo"
    remote_repo.mkdir()
    environment = _clean_environment()
    environment.update(
        {
            "LLM_API_KEY": "test-key",
            "LLM_MODEL": "provider/model",
            "OPENCOLLAB_OPENHANDS_PYTHON": "/usr/bin/true",
            "OPENCOLLAB_REMOTE_REPO": str(remote_repo),
        }
    )

    completed = subprocess.run(
        ["bash", str(SCRIPT), "--headless"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0


def test_openhands_launcher_pins_the_declared_optional_dependency() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'version("openhands") != "1.16.0"' in script
    assert "OPENCOLLAB_OPENHANDS_SITE" not in script
    assert "OPENCOLLAB_PYDEPS" not in script
