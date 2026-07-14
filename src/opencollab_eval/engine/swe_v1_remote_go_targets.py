"""Exact Go target-to-package derivation for ProLite plans."""

# ruff: noqa: F403, F405

from opencollab_eval.engine.swe_test_plan_contract import is_go_test_name
from opencollab_eval.engine.swe_v1_remote_state import *


def go_test_packages(tests, selected):
    packages = []
    for raw in tests or selected:
        item = str(raw or "").split(" | ", 1)[0].split("::", 1)[0].strip()
        if not item:
            continue
        if item.endswith(".go"):
            package = str(pathlib.Path(item).parent).replace("\\", "/")
        elif "/" in item:
            package = item.strip("/")
            if package and not package.endswith("..."):
                package = package.rstrip("/") + "/..."
        else:
            continue
        if package in {"", "."}:
            target = "./..."
        elif package.startswith("./"):
            target = package
        else:
            target = "./" + package
        if target not in packages:
            packages.append(target)
    return packages


def go_exact_test_spec(raw):
    """Map one declared Go node to an exact package and test event."""
    declared = str(raw or "").split(" | ", 1)[0].strip()
    if "::" not in declared:
        return None
    path, test_name = (part.strip() for part in declared.split("::", 1))
    if not path.endswith(".go") or not is_go_test_name(test_name):
        return None
    parent = str(pathlib.PurePosixPath(path.replace("\\", "/")).parent)
    if parent in {"", "."}:
        package = "."
    elif parent.startswith("./"):
        package = parent
    else:
        package = "./" + parent.strip("/")
    return {
        "declared_target": str(raw),
        "package": package,
        "test": test_name,
        "test_file": path.replace("\\", "/").removeprefix("./"),
        "run_pattern": "^" + re.escape(test_name) + "$",
    }




__all__ = ["go_exact_test_spec", "go_test_packages"]
