"""Behavior tests for trusted-base secret history scanning."""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "check_secret_history.py"
_ZERO_SHA = "0" * 40


def _subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{_REPO_ROOT}{os.pathsep}{existing}" if existing else str(_REPO_ROOT)
    )
    environment["PATH"] = os.pathsep.join(
        (str(Path(sys.executable).parent), environment.get("PATH", ""))
    )
    return environment


def _script_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_secret_history", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Security Test")
    _git(repository, "config", "user.email", "security@example.invalid")
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "base.txt")
    _git(repository, "commit", "-m", "chore: \u5efa\u7acb\u57fa\u7ebf")
    return repository, _git(repository, "rev-parse", "HEAD")


def _run(
    repository: Path,
    base: str,
    head: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "scripts.check_secret_history", base, head],
        cwd=repository,
        check=False,
        capture_output=True,
        env=_subprocess_environment(),
        text=True,
    )


def _secret() -> str:
    return "sk-" + "A" * 32


def _commit(repository: Path, path: str, content: str) -> None:
    (repository / path).write_text(content, encoding="utf-8")
    _git(repository, "add", path)
    _git(repository, "commit", "-m", "test: \u66f4\u65b0\u6d4b\u8bd5\u6587\u4ef6")


def test_secret_history_scans_secret_removed_by_later_commit(tmp_path):
    repository, base = _repository(tmp_path)
    _commit(repository, "temporary.txt", _secret() + "\n")
    (repository / "temporary.txt").unlink()
    _git(repository, "add", "-u")
    _git(repository, "commit", "-m", "test: \u5220\u9664\u6d4b\u8bd5\u6587\u4ef6")

    result = _run(repository, base, "HEAD")

    assert result.returncode == 1
    assert "Potential OpenAI API key introduced in commit" in result.stdout


def test_secret_history_trusts_only_existing_base_locations(tmp_path):
    repository, _base = _repository(tmp_path)
    _commit(repository, "existing.txt", _secret() + "\n")
    trusted_base = _git(repository, "rev-parse", "HEAD")
    _commit(repository, "safe.txt", "safe\n")

    assert _run(repository, trusted_base, "HEAD").returncode == 0

    _commit(repository, "copied.txt", _secret() + "\n")
    copied_result = _run(repository, trusted_base, "HEAD")

    assert copied_result.returncode == 1
    assert "file=copied.txt" in copied_result.stdout


def test_secret_history_rejects_duplicate_secret_in_the_same_base_path(tmp_path):
    repository, _base = _repository(tmp_path)
    _commit(repository, "existing.txt", _secret() + "\n")
    trusted_base = _git(repository, "rev-parse", "HEAD")
    _commit(repository, "existing.txt", _secret() + "\n" + _secret() + "\n")

    result = _run(repository, trusted_base, "HEAD")

    assert result.returncode == 1
    assert "file=existing.txt,line=2" in result.stdout


def test_secret_history_accepts_clean_commits_and_special_names(tmp_path):
    repository, base = _repository(tmp_path)
    _commit(repository, "clean\nfile.txt", "safe\n")

    result = _run(repository, base, "HEAD")

    assert result.returncode == 0
    assert "checks passed" in result.stdout


def test_secret_history_escapes_special_name_in_annotation(tmp_path):
    repository, base = _repository(tmp_path)
    _commit(repository, "secret\nfile.txt", _secret() + "\n")

    result = _run(repository, base, "HEAD")

    assert result.returncode == 1
    assert "file=secret%0Afile.txt,line=1" in result.stdout


def test_secret_history_rejects_private_keys_and_assigned_credentials(tmp_path):
    repository, base = _repository(tmp_path)
    content = (
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----\n"
        'client_secret = "' + "B" * 24 + '"\n'
    )
    _commit(repository, "credentials.txt", content)

    result = _run(repository, base, "HEAD")

    assert result.returncode == 1
    assert "Potential private key" in result.stdout
    assert "Potential assigned credential" in result.stdout


def test_secret_history_rejects_namespaced_assigned_credentials(tmp_path):
    repository, base = _repository(tmp_path)
    assignments = "\n".join(
        f'{name}="{"B" * 24}"'
        for name in (
            "GLM_API_KEY",
            "OPENAI_API_KEY",
            "DB_PASSWORD",
            "OAUTH_CLIENT_SECRET",
        )
    )
    _commit(repository, "credentials.env", assignments + "\n")

    result = _run(repository, base, "HEAD")

    assert result.returncode == 1
    assert result.stdout.count("Potential assigned credential") == 4


def test_secret_history_rejects_removed_namespaced_credential(tmp_path):
    repository, base = _repository(tmp_path)
    _commit(repository, "temporary.env", f'OPENAI_API_KEY="{"B" * 24}"\n')
    (repository / "temporary.env").unlink()
    _git(repository, "add", "-u")
    _git(repository, "commit", "-m", "test: remove temporary credential")

    result = _run(repository, base, "HEAD")

    assert result.returncode == 1
    assert "Potential assigned credential introduced in commit" in result.stdout


def test_secret_history_rejects_zero_commit_range(tmp_path):
    repository, base = _repository(tmp_path)

    result = _run(repository, base, base)

    assert result.returncode == 1
    assert "No proposed commits" in result.stdout


def test_secret_history_rejects_a_tree_without_files(tmp_path):
    repository = tmp_path / "empty"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Security Test")
    _git(repository, "config", "user.email", "security@example.invalid")
    _git(
        repository,
        "commit",
        "--allow-empty",
        "-m",
        "test: \u5efa\u7acb\u7a7a\u6811",
    )

    result = _run(repository, _ZERO_SHA, "HEAD")

    assert result.returncode == 2
    assert "has no files to scan" in result.stdout


def test_zero_sha_base_scans_all_reachable_commits(tmp_path):
    repository, _base = _repository(tmp_path)
    _commit(repository, "temporary.txt", _secret() + "\n")
    (repository / "temporary.txt").unlink()
    _git(repository, "add", "-u")
    _git(repository, "commit", "-m", "test: \u5220\u9664\u6d4b\u8bd5\u6587\u4ef6")

    result = _run(repository, _ZERO_SHA, "HEAD")

    assert result.returncode == 1
    assert "Potential OpenAI API key introduced" in result.stdout


def test_secret_history_reports_git_failure(tmp_path):
    repository, _base = _repository(tmp_path)

    result = _run(repository, "missing-base", "HEAD")

    assert result.returncode == 2
    assert "scan failed" in result.stdout


def test_secret_history_fails_when_scanner_raises(
    tmp_path,
    monkeypatch,
    capsys,
):
    repository, base = _repository(tmp_path)
    _commit(repository, "safe.txt", "safe\n")
    module = _script_module()

    def fail_scan(_content):
        raise ValueError("scanner crashed")

    monkeypatch.setattr(module, "_scan_blob", fail_scan)
    monkeypatch.setattr(
        module,
        "_arguments",
        lambda: argparse.Namespace(base=base, head="HEAD"),
    )
    monkeypatch.chdir(repository)

    result = module.main()

    assert result == 2
    assert "scanner crashed" in capsys.readouterr().out
