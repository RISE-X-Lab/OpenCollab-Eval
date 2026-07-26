"""Repository-level checks for the public OpenCollab-Eval surface."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(
    os.environ.get("OPENCOLLAB_EVAL_SOURCE_ROOT", Path(__file__).resolve().parents[1])
).resolve()
_PUBLIC_TEXT_SUFFIXES = {
    ".example",
    ".json",
    ".md",
    ".py",
    ".rst",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
_PUBLIC_TEXT_NAMES = {
    ".dockerignore",
    ".editorconfig",
    ".gitignore",
    "CODEOWNERS",
    "NOTICE",
}
_UNICODE_FIXTURES = {
    Path("tests/swe_v1_prolite_runner_evidence_patch_tests.py"),
    Path("tests/test_swe_final_report.py"),
}
_ACTION_REF = re.compile(r"^\s*(?:-\s+)?uses:\s+([^#\s]+)", re.MULTILINE)
_FULL_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_HOST_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]+"
    r"(?![A-Za-z0-9_.-])"
)
_REPOSITORY_TOKEN = re.compile(
    r"(?=([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))"
)
_BLOCKED_PUBLIC_TOKEN_DIGESTS = {
    "".join(
        (
            "79e5cce5",
            "77d2a144",
            "14c352a7",
            "8d2e9acd",
            "084296eb",
            "f7e16d7e",
            "2af40ccd",
            "f45aab5c",
        )
    ),
    "".join(
        (
            "73e9f675",
            "1ce13041",
            "8535236a",
            "5494d86b",
            "d80e28df",
            "6d246fcd",
            "a3ae605d",
            "2fe3b548",
        )
    ),
    "".join(
        (
            "ba1ec5f6",
            "0e46ff16",
            "edfc0a39",
            "15312816",
            "2ee345ba",
            "1088da29",
            "4b20ec83",
            "540414b9",
        )
    ),
    "".join(
        (
            "8806f14a",
            "1df6e4f8",
            "a66cf9d9",
            "0ad4ef61",
            "07af1a7e",
            "f2ff1220",
            "41ea1ec1",
            "f6d22dbe",
        )
    ),
}


def _repository_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return [
        _REPO_ROOT / entry.decode()
        for entry in completed.stdout.split(b"\0")
        if entry and (_REPO_ROOT / entry.decode()).is_file()
    ]


def _public_text_files() -> list[Path]:
    return [
        path
        for path in _repository_files()
        if path.name != "LICENSE"
        and (path.suffix in _PUBLIC_TEXT_SUFFIXES or path.name in _PUBLIC_TEXT_NAMES)
    ]


def _workflow_files(root: Path = _REPO_ROOT) -> list[Path]:
    workflows = root / ".github" / "workflows"
    return sorted({*workflows.glob("*.yml"), *workflows.glob("*.yaml")})


def test_public_text_is_english_and_uses_canonical_project_names() -> None:
    findings: list[str] = []

    for path in _public_text_files():
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(_REPO_ROOT)
        for line_number, line in enumerate(text.splitlines(), start=1):
            if relative not in _UNICODE_FIXTURES and any(
                "\u4e00" <= char <= "\u9fff" for char in line
            ):
                findings.append(f"{relative}:{line_number}: non-English public text")
            candidates = _HOST_TOKEN.findall(line)
            candidates.extend(
                match.group(1) for match in _REPOSITORY_TOKEN.finditer(line)
            )
            if any(
                hashlib.sha256(value.encode()).hexdigest()
                in _BLOCKED_PUBLIC_TOKEN_DIGESTS
                for value in candidates
            ):
                findings.append(
                    f"{relative}:{line_number}: blocked private identifier"
                )

    assert not findings, "\n".join(findings)


def test_distribution_metadata_points_to_the_canonical_repository() -> None:
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    canonical = "https://github.com/RISE-X-Lab/OpenCollab-Eval"

    assert 'authors = [{ name = "OpenCollab-Eval contributors" }]' in pyproject
    assert 'maintainers = [{ name = "RISE-X-Lab" }]' in pyproject
    assert f'Homepage = "{canonical}"' in pyproject
    assert f'Repository = "{canonical}"' in pyproject
    assert f'Issues = "{canonical}/issues"' in pyproject


def test_secret_baseline_is_fully_audited() -> None:
    workflow = (_REPO_ROOT / ".github/workflows/security.yml").read_text(
        encoding="utf-8"
    )
    baseline = json.loads(
        (_REPO_ROOT / ".secrets.baseline").read_text(encoding="utf-8")
    )

    assert baseline["version"] == "1.5.0"
    assert baseline["plugins_used"]
    assert baseline["results"]
    assert all(
        finding.get("is_secret") is False
        for findings in baseline["results"].values()
        for finding in findings
    )
    assert "detect-secrets==1.5.0" in workflow
    assert "scripts/check_secret_history.py" in workflow


def test_contributor_covenant_license_is_attributed() -> None:
    notices = (_REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    assert "Contributor Covenant version 2.1" in notices
    assert "License CC BY 4.0" in notices
    assert "https://creativecommons.org/licenses/by/4.0/" in notices


def test_workflow_actions_are_immutable_and_checkout_drops_credentials() -> None:
    workflow_files = _workflow_files()
    assert workflow_files

    for path in workflow_files:
        text = path.read_text(encoding="utf-8")
        for ref in _ACTION_REF.findall(text):
            if ref.startswith("./"):
                continue
            _action, separator, revision = ref.partition("@")
            assert separator and _FULL_GIT_SHA.fullmatch(revision), (path.name, ref)

        checkout_count = text.count("actions/checkout@")
        assert text.count("persist-credentials: false") == checkout_count, path.name
        assert "\npermissions:\n  contents: read\n" in text, path.name


def test_workflow_discovery_includes_yaml_and_yml(tmp_path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "first.yml").write_text("name: first\n", encoding="utf-8")
    (workflows / "second.yaml").write_text("name: second\n", encoding="utf-8")

    discovered = _workflow_files(tmp_path)

    assert [path.name for path in discovered] == ["first.yml", "second.yaml"]


def test_generated_evaluation_material_is_not_tracked() -> None:
    forbidden_suffixes = {".aux", ".log", ".pdf", ".xdv"}
    forbidden_names = {
        "predictions.jsonl",
        "report.json",
        "results.json",
    }
    findings = []
    for path in _repository_files():
        relative = path.relative_to(_REPO_ROOT)
        if path.suffix in forbidden_suffixes or path.name in forbidden_names:
            findings.append(str(relative))
        if "docs/reports" in relative.as_posix():
            findings.append(str(relative))
    assert not findings, "\n".join(findings)
