from __future__ import annotations

import ast
import importlib
from pathlib import Path

import opencollab_eval

_TEST_ROOT = Path(__file__).resolve().parent
_SOURCE = Path(opencollab_eval.__file__).resolve().parent


def _forbidden_opencollab_imports(tree: ast.AST) -> list[str]:
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name
                sdk_import = module == "opencollab.sdk" or module.startswith("opencollab.sdk.")
                if module == "opencollab" or (module.startswith("opencollab.") and not sdk_import):
                    offenders.append(module)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
            if module == "opencollab":
                offenders.extend(f"{module}.{alias.name}" for alias in node.names if alias.name != "sdk")
            elif module.startswith("opencollab.") and not (
                module == "opencollab.sdk" or module.startswith("opencollab.sdk.")
            ):
                offenders.append(module)
    return offenders


def test_eval_code_imports_opencollab_only_through_sdk() -> None:
    offenders: list[str] = []
    paths = (*sorted(_SOURCE.rglob("*.py")), *sorted(_TEST_ROOT.rglob("*.py")))
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(f"{path}: {module}" for module in _forbidden_opencollab_imports(tree))
    assert offenders == []


def test_sdk_import_boundary_rejects_root_and_internal_import_forms() -> None:
    forbidden = ast.parse(
        "import opencollab\n"
        "import opencollab.application\n"
        "from opencollab import application\n"
        "from opencollab.adapters import tools\n"
    )
    allowed = ast.parse(
        "import opencollab.sdk\n"
        "from opencollab import sdk\n"
        "from opencollab.sdk import SDK_API_VERSION\n"
        "from opencollab.sdk.eval_compat import LocalEnv\n"
    )

    assert _forbidden_opencollab_imports(forbidden) == [
        "opencollab",
        "opencollab.application",
        "opencollab.application",
        "opencollab.adapters",
    ]
    assert _forbidden_opencollab_imports(allowed) == []


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


def test_evaluation_strategy_is_owned_by_eval_package() -> None:
    fact_sheet = (_SOURCE / "workflows" / "_fact_sheet.py").read_text(encoding="utf-8")
    analyst_runtime = (_SOURCE / "workflows" / "_analyst_solve_runtime.py").read_text(
        encoding="utf-8"
    )

    assert "opencollab" not in fact_sheet
    assert "from ._fact_sheet import" in analyst_runtime
    assert "sdk.experimental import (\n    build_fact_sheet" not in analyst_runtime


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
