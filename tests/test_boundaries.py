from __future__ import annotations

import ast
import importlib
from pathlib import Path

import opencollab_eval

_TEST_ROOT = Path(__file__).resolve().parent
_SOURCE = Path(opencollab_eval.__file__).resolve().parent


def test_eval_code_imports_opencollab_only_through_sdk() -> None:
    offenders: list[str] = []
    paths = (*sorted(_SOURCE.rglob("*.py")), *sorted(_TEST_ROOT.rglob("*.py")))
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            for module in modules:
                if module.startswith("opencollab.") and not module.startswith("opencollab.sdk"):
                    offenders.append(f"{path}: {module}")
    assert offenders == []


def test_eval_compat_imports_exist_in_installed_sdk() -> None:
    required: set[str] = set()
    paths = (*sorted(_SOURCE.rglob("*.py")), *sorted(_TEST_ROOT.rglob("*.py")))
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "opencollab.sdk.eval_compat":
                required.update(alias.name for alias in node.names)

    compatibility = importlib.import_module("opencollab.sdk.eval_compat")
    missing = sorted(name for name in required if not hasattr(compatibility, name))
    assert missing == []


def test_workflow_documentation_uses_packaged_layout() -> None:
    readme = (_SOURCE / "workflows" / "README.md").read_text(encoding="utf-8")
    stale_layout_fragments = (
        "swebench/gen_prediction_workflow.py",
        "OPENCOLLAB_WORKFLOWS_DIR",
        "`opencollab workflow",
        "opencollab/.venv",
        "`application/workflow.py`",
        "`opencollab/adapters/tools/`",
        "`workflows/` relative to the working directory",
    )

    assert [fragment for fragment in stale_layout_fragments if fragment in readme] == []
