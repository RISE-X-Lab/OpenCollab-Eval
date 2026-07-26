"""Behavior tests for trusted-base secret history scanning."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(
    os.environ.get("OPENCOLLAB_EVAL_SOURCE_ROOT", Path(__file__).resolve().parents[1])
).resolve()
_SCRIPT = _REPO_ROOT / "scripts" / "check_secret_history.py"
_BASELINE = _REPO_ROOT / ".secrets.baseline"
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
    shutil.copyfile(_BASELINE, repository / ".secrets.baseline")
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", ".secrets.baseline", "base.txt")
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


def test_secret_history_rejects_personal_environment_details(tmp_path):
    repository, base = _repository(tmp_path)
    content = "\n".join(
        (
            "workspace = /" + "Users/alice/project",
            "volume = /" + "Volumes/private-data/results",
            "owner = alice" + "@gmail.com",
            "host = 192" + ".168.12.34",
        )
    )
    _commit(repository, "environment.txt", content + "\n")

    result = _run(repository, base, "HEAD")

    assert result.returncode == 1
    assert "Potential personal macOS path" in result.stdout
    assert "Potential personal email" in result.stdout
    assert "Potential private IPv4 address" in result.stdout


def test_secret_history_rejects_zero_commit_range(tmp_path):
    repository, base = _repository(tmp_path)

    result = _run(repository, base, base)

    assert result.returncode == 1
    assert "No proposed commits" in result.stdout


def test_secret_history_rejects_missing_baseline(tmp_path):
    repository, base = _repository(tmp_path)
    (repository / ".secrets.baseline").unlink()
    _git(repository, "add", "-u")
    _git(repository, "commit", "-m", "test: remove secret baseline")

    result = _run(repository, base, "HEAD")

    assert result.returncode == 1
    assert ".secrets.baseline is missing" in result.stdout


def test_secret_history_rejects_unaudited_baseline_change(tmp_path):
    repository, base = _repository(tmp_path)
    baseline_path = repository / ".secrets.baseline"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["plugins_used"].append({"name": "UnexpectedDetector"})
    baseline_path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    _git(repository, "add", ".secrets.baseline")
    _git(repository, "commit", "-m", "test: modify secret baseline")

    result = _run(repository, base, "HEAD")

    assert result.returncode == 1
    assert "changed without a digest approved" in result.stdout


def test_secret_history_rejects_intermediate_baseline_change_then_restore(tmp_path):
    repository, base = _repository(tmp_path)
    baseline_path = repository / ".secrets.baseline"
    trusted_content = baseline_path.read_bytes()
    baseline = json.loads(trusted_content)
    baseline["plugins_used"].append({"name": "IntermediateDetector"})
    baseline_path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    _git(repository, "add", ".secrets.baseline")
    _git(repository, "commit", "-m", "test: change baseline temporarily")
    baseline_path.write_bytes(trusted_content)
    _git(repository, "add", ".secrets.baseline")
    _git(repository, "commit", "-m", "test: restore trusted baseline")

    result = _run(repository, base, "HEAD")

    assert result.returncode == 1
    assert "trusted base checker in commit" in result.stdout


def test_zero_sha_allows_history_before_approved_baseline(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Security Test")
    _git(repository, "config", "user.email", "security@example.invalid")
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "base.txt")
    _git(repository, "commit", "-m", "test: create source tree")
    shutil.copyfile(_BASELINE, repository / ".secrets.baseline")
    _git(repository, "add", ".secrets.baseline")
    _git(repository, "commit", "-m", "test: add approved baseline")
    module = _script_module()
    monkeypatch.setattr(module, "_verify_detect_secrets", lambda: None)
    monkeypatch.setattr(
        module,
        "_detect_secrets_identities",
        lambda *_args: set(),
    )
    monkeypatch.setattr(
        module,
        "_scan_tree_with_detect_secrets",
        lambda *_args: True,
    )

    assert module.check_secret_history(repository, _ZERO_SHA, "HEAD") == 0


def test_secret_history_accepts_digest_approved_by_trusted_checker(
    tmp_path,
    monkeypatch,
):
    repository, base = _repository(tmp_path)
    baseline_path = repository / ".secrets.baseline"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["generated_at"] = "2099-01-01T00:00:00Z"
    baseline_path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    _git(repository, "add", ".secrets.baseline")
    _git(repository, "commit", "-m", "test: update generated timestamp")
    module = _script_module()
    digest = module._baseline_digest(baseline_path.read_bytes())
    monkeypatch.setattr(module, "_APPROVED_BASELINE_SHA256", {digest})
    monkeypatch.setattr(module, "_verify_detect_secrets", lambda: None)
    monkeypatch.setattr(
        module,
        "_detect_secrets_identities",
        lambda *_args: set(),
    )
    monkeypatch.setattr(
        module,
        "_scan_tree_with_detect_secrets",
        lambda *_args: True,
    )

    assert module.check_secret_history(repository, base, "HEAD") == 0


def test_secret_history_rejects_payload_in_generated_timestamp(tmp_path):
    repository, base = _repository(tmp_path)
    baseline_path = repository / ".secrets.baseline"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["generated_at"] = "random-credential-" + "X" * 64
    baseline_path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    _git(repository, "add", ".secrets.baseline")
    _git(repository, "commit", "-m", "test: hide payload in generated timestamp")

    result = _run(repository, base, "HEAD")

    assert result.returncode == 2
    assert "generated_at must be a UTC timestamp" in result.stdout


def test_secret_history_rejects_duplicate_generated_timestamp(tmp_path):
    repository, base = _repository(tmp_path)
    baseline_path = repository / ".secrets.baseline"
    trusted = baseline_path.read_text(encoding="utf-8")
    hidden_value = "concealed-" + "Y" * 64
    baseline_path.write_text(
        trusted.replace("{", f'{{\n  "generated_at": "{hidden_value}",', 1),
        encoding="utf-8",
    )
    _git(repository, "add", ".secrets.baseline")
    _git(repository, "commit", "-m", "test: duplicate generated timestamp")

    result = _run(repository, base, "HEAD")

    assert result.returncode == 2
    assert "repeats JSON key 'generated_at'" in result.stdout


def test_secret_history_rejects_unreviewed_baseline_finding(tmp_path):
    repository, base = _repository(tmp_path)
    baseline_path = repository / ".secrets.baseline"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    first_path = next(iter(baseline["results"]))
    baseline["results"][first_path][0]["is_secret"] = None
    baseline_path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    _git(repository, "add", ".secrets.baseline")
    _git(repository, "commit", "-m", "test: remove audit verdict")

    result = _run(repository, base, "HEAD")

    assert result.returncode == 2
    assert "audited false verdict" in result.stdout


def test_detect_secrets_scans_high_entropy_value_removed_later(tmp_path):
    repository, base = _repository(tmp_path)
    _commit(
        repository,
        "temporary.txt",
        'digest = "d8f391c72a6be4059c18f7a2d63b4e91"\n',
    )
    (repository / "temporary.txt").unlink()
    _git(repository, "add", "-u")
    _git(repository, "commit", "-m", "test: remove high entropy fixture")

    result = _run(repository, base, "HEAD")

    assert result.returncode == 1
    assert "detect-secrets found an unaudited finding" in result.stdout


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
