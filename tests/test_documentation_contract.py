from __future__ import annotations

import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

import pytest

from opencollab_eval import cli
from opencollab_eval.engine.solver_backend import DEFAULT_WORKFLOW_SOLVERS

ROOT = Path(
    os.environ.get("OPENCOLLAB_EVAL_SOURCE_ROOT", Path(__file__).resolve().parents[1])
).resolve()
DOCS = ROOT / "docs"
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
FENCED_CODE = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
HEADING = re.compile(r"^(#{1,6}) ", re.MULTILINE)
INLINE_CODE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")

ROOT_DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "MIGRATION.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
)
SOURCE_DOCUMENTS = (
    ROOT / "src" / "opencollab_eval" / "engine" / "eval_adapter" / "README.md",
    ROOT / "src" / "opencollab_eval" / "workflows" / "README.md",
)


def _english_docs() -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(DOCS.rglob("*.md"))
        if "zh-CN" not in path.relative_to(DOCS).parts
    )


def _chinese_counterpart(path: Path) -> Path:
    if path in ROOT_DOCUMENTS:
        return path.with_name(f"{path.stem}.zh-CN.md")
    if path in SOURCE_DOCUMENTS:
        return path.with_name("README.zh-CN.md")
    return DOCS / "zh-CN" / path.relative_to(DOCS)


def _bilingual_pairs() -> tuple[tuple[Path, Path], ...]:
    english = (*ROOT_DOCUMENTS, *SOURCE_DOCUMENTS, *_english_docs())
    return tuple((path, _chinese_counterpart(path)) for path in english)


def _documentation_files() -> tuple[Path, ...]:
    return tuple(
        dict.fromkeys(
            path
            for english, chinese in _bilingual_pairs()
            for path in (english, chinese)
        )
    )


def _chinese_documents() -> set[Path]:
    return {
        *ROOT.glob("*.zh-CN.md"),
        *(DOCS / "zh-CN").rglob("*.md"),
        *(ROOT / "src" / "opencollab_eval").rglob("README.zh-CN.md"),
    }


def _inline_code_tokens(text: str) -> Counter[str]:
    return Counter(INLINE_CODE.findall(FENCED_CODE.sub("", text)))


def _resolved_local_links(path: Path) -> set[Path]:
    links: set[Path] = set()
    for raw_target in MARKDOWN_LINK.findall(path.read_text(encoding="utf-8")):
        target = raw_target.strip().strip("<>").split("#", 1)[0]
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        links.add((path.parent / unquote(target)).resolve())
    return links


@pytest.mark.parametrize(
    "path",
    _documentation_files(),
    ids=lambda path: str(path.relative_to(ROOT)),
)
def test_local_markdown_links_resolve(path: Path) -> None:
    missing: list[str] = []
    for raw_target in MARKDOWN_LINK.findall(path.read_text(encoding="utf-8")):
        target = raw_target.strip().strip("<>").split("#", 1)[0]
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        resolved = (path.parent / unquote(target)).resolve()
        if not resolved.exists():
            missing.append(raw_target)
    assert missing == []


def test_documentation_index_references_every_document() -> None:
    index = (DOCS / "README.md").read_text(encoding="utf-8")
    missing = [
        path.relative_to(DOCS).as_posix()
        for path in _english_docs()
        if path.name != "README.md" and f"]({path.relative_to(DOCS).as_posix()})" not in index
    ]
    assert missing == []


def test_chinese_documentation_index_references_every_document() -> None:
    chinese_root = DOCS / "zh-CN"
    index = (chinese_root / "README.md").read_text(encoding="utf-8")
    missing = [
        path.relative_to(chinese_root).as_posix()
        for path in sorted(chinese_root.rglob("*.md"))
        if path != chinese_root / "README.md"
        and f"]({path.relative_to(chinese_root).as_posix()})" not in index
    ]
    assert missing == []


def test_every_chinese_document_has_a_canonical_english_source() -> None:
    expected = {chinese for _english, chinese in _bilingual_pairs()}
    assert _chinese_documents() == expected


@pytest.mark.parametrize(
    ("english", "chinese"),
    _bilingual_pairs(),
    ids=lambda value: str(value.relative_to(ROOT)),
)
def test_bilingual_documents_are_linked_and_structurally_aligned(
    english: Path,
    chinese: Path,
) -> None:
    assert chinese.is_file()
    english_text = english.read_text(encoding="utf-8")
    chinese_text = chinese.read_text(encoding="utf-8")
    language_name = "\u7b80\u4f53\u4e2d\u6587"
    assert english_text.splitlines()[2].startswith(
        f"**English** | [{language_name}]("
    )
    assert chinese_text.splitlines()[2].startswith("[English](")
    assert chinese_text.splitlines()[2].endswith(f"| **{language_name}**")
    assert chinese.resolve() in _resolved_local_links(english)
    assert english.resolve() in _resolved_local_links(chinese)
    assert FENCED_CODE.findall(chinese_text) == FENCED_CODE.findall(english_text)
    assert HEADING.findall(chinese_text) == HEADING.findall(english_text)
    assert _inline_code_tokens(chinese_text) == _inline_code_tokens(english_text)
    assert any("\u4e00" <= char <= "\u9fff" for char in chinese_text)


@pytest.mark.parametrize(
    ("english", "chinese"),
    _bilingual_pairs(),
    ids=lambda value: str(value.relative_to(ROOT)),
)
def test_chinese_internal_links_prefer_available_chinese_documents(
    english: Path,
    chinese: Path,
) -> None:
    counterparts = dict(_bilingual_pairs())
    english_links = _resolved_local_links(english)
    chinese_links = _resolved_local_links(chinese)
    for target in english_links:
        if target in counterparts:
            assert counterparts[target] in chinese_links
    assert not {
        target for target in chinese_links if target in counterparts and target != english
    }


def test_readme_names_installed_commands_and_solver_profiles() -> None:
    for path in (ROOT / "README.md", ROOT / "README.zh-CN.md"):
        readme = path.read_text(encoding="utf-8")
        for command in ("inspect", "run", "swe-v1-prolite", "final-report"):
            assert f"`oc-eval {command}`" in readme
        for solver in DEFAULT_WORKFLOW_SOLVERS:
            assert f"`{solver}`" in readme


def test_documented_kimi_slice_has_complete_identity() -> None:
    required = (
        "--context-window 262144",
        "--temperature 1",
        "--top-p 0.95",
        "--max-output-tokens 32768",
        "--workflow-env OPENCOLLAB_THINKING=true",
        '"thinking":{"type":"enabled","keep":"all"}',
    )
    for relative in (
        "README.md",
        "README.zh-CN.md",
        "docs/swe-prolite-operations.md",
        "docs/zh-CN/swe-prolite-operations.md",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert all(value in text for value in required)


def test_coordinator_example_uses_only_forwarded_options() -> None:
    operations = (DOCS / "swe-prolite-operations.md").read_text(encoding="utf-8")
    section = operations.split("## Run through the Solver coordinator", 1)[1]
    example = section.split("```bash", 1)[1].split("```", 1)[0]
    assert "--remote-python /srv/opencollab-eval/venv/bin/python" in example


def test_documented_k3_coordinator_has_complete_identity() -> None:
    required = (
        "--llm-model k3",
        "--context-window 1048576",
        "--temperature 1",
        "--top-p 0.95",
        "--max-output-tokens 32768",
        "reasoning_effort=high",
    )
    for relative in (
        "README.md",
        "README.zh-CN.md",
        "docs/swe-prolite-operations.md",
        "docs/zh-CN/swe-prolite-operations.md",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert all(value in text for value in required)


def test_removed_documentation_claims_do_not_return() -> None:
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in _documentation_files())
    for stale in (
        "Run the migrated JSONL evaluation engine",
        ".publication.json",
        "docs/plans/",
        "Python rows currently derive an unsupported plan",
    ):
        assert stale not in corpus


def test_top_level_help_describes_candidate_eligibility() -> None:
    help_text = cli.build_parser().format_help()
    assert "submission eligibility" in help_text
    assert "migrated JSONL" not in help_text


def test_prolite_help_documents_operator_options() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "opencollab_eval", "swe-v1-prolite", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    for value in (
        "SSH destination for the Linux worker",
        "--remote-python",
        "--remote-api-env-file",
        "--expected-runtime-tree-sha256",
        "--dry-run",
    ):
        assert value in result.stdout


@pytest.mark.parametrize(
    ("script", "usage"),
    (
        ("run_deterministic_swe_e2e.sh", "--output DIRECTORY"),
        ("verify_wheel_contract.sh", "PATH_TO_OPENCOLLAB_WHEEL"),
    ),
)
def test_public_scripts_offer_side_effect_free_help(
    script: str,
    usage: str,
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / script), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert usage in result.stdout
    assert list(tmp_path.iterdir()) == []
