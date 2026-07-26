from __future__ import annotations

import os
from pathlib import Path

_IGNORED_PARTS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__", "build", "dist"}
_REQUIRED_FILES = {
    ".github/workflows/ci.yml",
    ".github/workflows/hygiene.yml",
    "docs/integrity-coverage.json",
    "scripts/verify_wheel_contract.sh",
}
_COMPILED_DOCUMENT_SUFFIXES = (
    ".aux",
    ".fdb_latexmk",
    ".fls",
    ".log",
    ".out",
    ".pdf",
    ".synctex.gz",
    ".xdv",
)


def _repository_root() -> Path:
    configured = os.environ.get("OPENCOLLAB_EVAL_SOURCE_ROOT")
    root = Path(configured).expanduser() if configured else Path(__file__).resolve().parents[1]
    return root.resolve()


def test_repository_python_files_stay_within_line_limit() -> None:
    root = _repository_root()
    assert not [path for path in _REQUIRED_FILES if not (root / path).is_file()]
    oversized = {}
    for path in root.rglob("*.py"):
        if _IGNORED_PARTS.intersection(path.relative_to(root).parts):
            continue
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count > 800:
            oversized[path.relative_to(root).as_posix()] = line_count
    assert oversized == {}


def test_repository_tracks_document_sources_only() -> None:
    root = _repository_root()
    offenders = sorted(
        path.relative_to(root).as_posix()
        for path in (root / "docs").rglob("*")
        if path.is_file() and path.name.endswith(_COMPILED_DOCUMENT_SUFFIXES)
    )
    assert offenders == []


def test_wheel_contract_copies_repository_test_support() -> None:
    root = _repository_root()
    script = (root / "scripts" / "verify_wheel_contract.sh").read_text(
        encoding="utf-8"
    )

    assert 'cp -R "$repo_root/scripts" "$venv_dir/scripts"' in script
    assert 'PYTHONPATH="$venv_dir:$venv_dir/eval-tests"' in script
    assert "detect-secrets==1.5.0" in script
