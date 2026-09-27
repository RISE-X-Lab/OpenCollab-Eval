"""Exercise the installed SWE-bench executor with a local container transport."""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.deterministic_swe_driver import TARGET_TEST, _official_eval, _test_patch
from tests.e2e.fake_openai_server import SOURCE_PATH


class LocalContainer:
    """Run the official executor's commands against an isolated local Git tree."""

    id = "local-swebench-transport"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspace = root / "testbed"
        self.workspace.mkdir(parents=True)
        self.calls: list[str] = []

    def start(self) -> None:
        pass

    def command(self, command: str) -> str:
        return command.replace("/tmp/patch.diff", shlex.quote(str(self.root / "tmp/patch.diff"))).replace(
            "/eval.sh", shlex.quote(str(self.root / "eval.sh"))
        )

    def exec_run(self, command, **_kwargs):
        self.calls.append(str(command))
        args = command if isinstance(command, list) else ["bash", "-c", self.command(command)]
        result = subprocess.run(args, cwd=self.workspace, text=False, capture_output=True, check=False)
        return SimpleNamespace(exit_code=result.returncode, output=result.stdout + result.stderr)

    def copy(self, source: Path, destination: Path) -> None:
        target = self.root / str(destination).lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        if str(destination) == "/eval.sh":
            text = source.read_text().replace("cd /testbed", f"cd {shlex.quote(str(self.workspace))}")
            text = text.replace(
                "source /opt/miniconda3/bin/activate testbed",
                f"export PATH={shlex.quote(str(Path(sys.executable).parent))}:\"$PATH\"",
            )
            target.write_text(text)
        else:
            shutil.copy2(source, target)

    def execute(self, command: str, timeout: float):
        result = subprocess.run(
            ["bash", "-c", self.command(command)],
            cwd=self.workspace,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return result.stdout + result.stderr, False, 0.0


@pytest.mark.parametrize("correct_patch", [True, False])
def test_real_official_executor_grades_and_binds_the_exact_candidate(monkeypatch, tmp_path, correct_patch):
    pytest.importorskip("swebench")
    import docker
    from swebench.harness import run_evaluation

    container = LocalContainer(tmp_path / "container")
    source = container.workspace / SOURCE_PATH
    source.parent.mkdir(parents=True)
    source.write_text("def add(left, right):\n    return left - right\n")
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "fixture@example.invalid"],
        ["git", "config", "user.name", "Fixture"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "\u521b\u5efa\u6d4b\u8bd5\u6837\u672c"],
    ):
        subprocess.run(command, cwd=container.workspace, capture_output=True, check=True)
    source.write_text(
        "def add(left, right):\n    return left + right\n"
        if correct_patch
        else "def add(left, right):\n    return left * right\n"
    )
    patch = subprocess.check_output(["git", "diff"], cwd=container.workspace, text=True)
    subprocess.run(["git", "checkout", "--", "."], cwd=container.workspace, capture_output=True, check=True)
    iid = "deterministic-calculator-fixture"
    prediction = {
        "instance_id": iid,
        "model_name_or_path": "local/model",
        "model_patch": patch,
        "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
    }
    instance = {
        "instance_id": iid,
        "repo": "pytest-dev/pytest",
        "version": "8.3",
        "test_patch": _test_patch(),
        "FAIL_TO_PASS": [TARGET_TEST],
        "PASS_TO_PASS": [],
    }
    seen = []

    def create(spec, _client, _run_id, _logger):
        seen.append(spec)
        return container

    monkeypatch.setattr(docker, "from_env", lambda: object())
    monkeypatch.setattr(run_evaluation, "create_container", create)
    monkeypatch.setattr(run_evaluation, "copy_to_container", lambda _container, path, dest: container.copy(path, dest))
    monkeypatch.setattr(
        run_evaluation, "exec_run_with_timeout", lambda _container, cmd, timeout: container.execute(cmd, timeout)
    )
    monkeypatch.setattr(run_evaluation, "cleanup_container", lambda *_args: None)
    workspace = tmp_path / "official-work"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    kwargs = dict(
        architecture="x86_64", namespace="fixture", run_id="real-harness", workspace=workspace, artifact_dir=artifacts
    )
    if correct_patch:
        proof = _official_eval(instance, prediction, **kwargs)
        assert proof["resolved"] is True
        assert proof["collected_tests"] == 1
        assert proof["patch_sha256"] == prediction["patch_sha256"]
        assert seen[0].image == "fixture/sweb.eval.x86_64.deterministic-calculator-fixture:latest"
        report = json.loads((artifacts / "official-report.json").read_text())
        assert report[iid]["resolved"] is True
        assert report[iid]["tests_status"]["FAIL_TO_PASS"]["success"] == [TARGET_TEST]
    else:
        with pytest.raises(RuntimeError, match="execution proof"):
            _official_eval(instance, prediction, **kwargs)
        report = json.loads((artifacts / "official-report.json").read_text())
        assert report[iid]["resolved"] is False
        assert report[iid]["tests_status"]["FAIL_TO_PASS"]["failure"] == [TARGET_TEST]
