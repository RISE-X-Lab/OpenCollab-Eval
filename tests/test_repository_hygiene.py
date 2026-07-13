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
