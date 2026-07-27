"""Repository-level checks for the public OpenCollab-Eval surface."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
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
_ENGLISH_LANGUAGE_SWITCH = re.compile(
    "^[*][*]English[*][*] [|] "
    r"\[\u7b80\u4f53\u4e2d\u6587\]\([^)]+\)"
    r"(?: [|] \[Documentation\]\([^)]+\))?$"
)
_CHINESE_LANGUAGE_OPTION = re.compile(r"^\s+- name: \u7b80\u4f53\u4e2d\u6587$")


def _is_simplified_chinese_document(relative: Path) -> bool:
    return (
        relative.name.endswith(".zh-CN.md")
        or relative.name == "mkdocs.zh-CN.yml"
        or relative.parts[:2] == ("docs", "zh-CN")
    )


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
    forbidden = (
        "Yihong" + "Dong/OpenCollab",
        "docker." + "1panel.live",
        "api." + "cherr.cc",
        "172.16." + "200.37",
    )
    findings: list[str] = []

    for path in _public_text_files():
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(_REPO_ROOT)
        for line_number, line in enumerate(text.splitlines(), start=1):
            if (
                relative not in _UNICODE_FIXTURES
                and not _is_simplified_chinese_document(relative)
                and not _ENGLISH_LANGUAGE_SWITCH.fullmatch(line)
                and not _CHINESE_LANGUAGE_OPTION.fullmatch(line)
                and any("\u4e00" <= char <= "\u9fff" for char in line)
            ):
                findings.append(f"{relative}:{line_number}: non-English public text")
            for value in forbidden:
                if value in line:
                    findings.append(
                        f"{relative}:{line_number}: private or stale value {value!r}"
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


def test_secret_gate_has_no_generated_baseline_or_runtime_scanner_download() -> None:
    workflow = (_REPO_ROOT / ".github/workflows/security.yml").read_text(
        encoding="utf-8"
    )
    scanner = (_REPO_ROOT / "scripts/check_secret_history.py").read_text(
        encoding="utf-8"
    )

    assert not (_REPO_ROOT / ".secrets.baseline").exists()
    assert ".secrets.baseline" not in workflow
    assert ".secrets.baseline" not in scanner
    assert "detect-secrets" not in workflow


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
