from __future__ import annotations

import ast
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[1] / "src" / "opencollab_eval"


def test_production_code_imports_opencollab_only_through_sdk() -> None:
    offenders: list[str] = []
    for path in sorted(_SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            for module in modules:
                if module.startswith("opencollab.") and not module.startswith("opencollab.sdk"):
                    offenders.append(f"{path.relative_to(_SOURCE)}: {module}")
    assert offenders == []

