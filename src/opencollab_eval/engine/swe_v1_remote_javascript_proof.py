"""JavaScript target binding and structured suite-failure proof helpers."""

# ruff: noqa: E501, F403, F405

from opencollab_eval.engine.swe_v1_remote_records import *
from opencollab_eval.engine.swe_v1_remote_state import *

_JS_REPOSITORY_MODULE_NAMESPACES = {
    "protonmail/webclients": ("@proton/",),
}


def _js_path_matches(left, right):
    left = str(left or "").replace("\\", "/").removeprefix("./")
    right = str(right or "").replace("\\", "/").removeprefix("./")
    return bool(
        left
        and right
        and (left == right or left.endswith("/" + right) or right.endswith("/" + left))
    )


def _js_suite_module_mock_bindings(row, target_files):
    namespaces = _JS_REPOSITORY_MODULE_NAMESPACES.get(
        str(row.get("repo") or "").lower()
    )
    if not namespaces:
        return []
    bindings = []
    for block in split_patch_blocks(str(row.get("test_patch") or "")):
        path = diff_target_path(block[0] if block else "")
        if not any(_js_path_matches(path, target) for target in target_files):
            continue
        modules = []
        for line in block:
            if not line.startswith("+") or line.startswith("+++"):
                continue
            match = re.search(r"\bjest\.(?:doMock|mock)\(\s*(['\"])([^'\"]+)\1", line)
            if not match:
                continue
            module = match.group(2)
            if module.startswith(namespaces) and module not in modules:
                modules.append(module)
        if modules:
            bindings.append({"suite": path, "modules": modules})
    return bindings


def _js_suite_load_failure_proof_matches(
    proof,
    log_text,
    expected_command,
    observed_command,
):
    if not expected_command or expected_command != observed_command:
        return False
    repo = str(proof.get("repo") or "").lower()
    namespaces = _JS_REPOSITORY_MODULE_NAMESPACES.get(repo)
    bindings = proof.get("suite_module_mocks")
    targets = proof.get("targets")
    if (
        not namespaces
        or not isinstance(bindings, list)
        or not bindings
        or len(bindings) > 64
        or not isinstance(targets, list)
        or not targets
    ):
        return False
    target_suites = {
        str(target).split(" | ", 1)[0].replace("\\", "/").removeprefix("./")
        for target in targets
    }
    if len(target_suites) != 1:
        return False
    target_suite = next(iter(target_suites))
    bound_modules = []
    bound_suite = ""
    for binding in bindings:
        if not isinstance(binding, dict) or not _js_path_matches(
            binding.get("suite"), target_suite
        ):
            continue
        modules = binding.get("modules")
        if (
            bound_suite
            or not isinstance(modules, list)
            or not modules
            or len(modules) > 128
            or len(set(modules)) != len(modules)
            or any(
                not isinstance(module, str)
                or not module.startswith(namespaces)
                or len(module.encode("utf-8")) > 4096
                for module in modules
            )
        ):
            return False
        bound_suite = str(binding.get("suite") or "")
        bound_modules = modules
    if not bound_suite:
        return False
    reports = []
    for line in str(log_text or "").splitlines():
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and "numTotalTestSuites" in value:
            reports.append(value)
    if len(reports) != 1:
        return False
    report = reports[0]
    results = report.get("testResults")
    if (
        report.get("success") is not False
        or report.get("numFailedTestSuites") != 1
        or report.get("numRuntimeErrorTestSuites") != 1
        or report.get("numTotalTestSuites") != 1
        or report.get("numTotalTests") != 0
        or not isinstance(results, list)
        or len(results) != 1
        or not isinstance(results[0], dict)
        or results[0].get("status") != "failed"
        or results[0].get("assertionResults") != []
        or not _js_path_matches(results[0].get("name"), bound_suite)
    ):
        return False
    message = str(results[0].get("message") or "")
    missing = re.search(
        r"Cannot find module '([^']+)' from '([^']+)'",
        message,
    )
    if (
        not missing
        or "Test suite failed to run" not in message
        or missing.group(1) not in bound_modules
        or not missing.group(1).startswith(namespaces)
        or not _js_path_matches(missing.group(2), target_suite)
    ):
        return False
    return re.search(
        r"(?m)^FAIL\s+" + re.escape(bound_suite) + r"\s*$",
        str(log_text or ""),
    ) is not None




__all__ = [
    "_JS_REPOSITORY_MODULE_NAMESPACES",
    "_js_path_matches",
    "_js_suite_load_failure_proof_matches",
    "_js_suite_module_mock_bindings",
]
