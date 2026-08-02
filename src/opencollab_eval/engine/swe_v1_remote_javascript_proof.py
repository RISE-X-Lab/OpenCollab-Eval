"""JavaScript target binding and structured suite-failure proof helpers."""

# ruff: noqa: E501, F403, F405

from opencollab_eval.engine.swe_v1_remote_records import *
from opencollab_eval.engine.swe_v1_remote_state import *


def _js_path_matches(left, right):
    left = str(left or "").replace("\\", "/").removeprefix("./")
    right = str(right or "").replace("\\", "/").removeprefix("./")
    return bool(
        left
        and right
        and (left == right or left.endswith("/" + right) or right.endswith("/" + left))
    )


def _js_suite_module_mock_bindings(row, target_files):
    bindings = []
    for block in split_patch_blocks(str(row.get("test_patch") or "")):
        path = patch_block_target_path(block)
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
            if (
                module
                and len(module.encode("utf-8")) <= 4096
                and module not in modules
            ):
                modules.append(module)
        if modules:
            bindings.append({"suite": path, "modules": modules})
    return bindings


def _js_test_patch_files(row):
    files = []
    for block in split_patch_blocks(str(row.get("test_patch") or "")):
        path = patch_block_target_path(block)
        if path and path not in files:
            files.append(path)
    return files


def _js_candidate_module_bindings(candidate_patch, candidate_source_paths):
    def line_modules(source):
        static = re.match(
            r"\s*(?:import|export)\s+(?:.*?\s+from\s+)?['\"]([^'\"]+)['\"]",
            source,
        )
        calls = re.findall(
            r"\b(?:require|import)\(\s*['\"]([^'\"]+)['\"]\s*\)",
            source,
        )
        return ([static.group(1)] if static else []) + calls

    candidate_paths = set(candidate_source_paths or [])
    bindings = []
    for block in split_patch_blocks(str(candidate_patch or "")):
        path = patch_block_target_path(block)
        if path not in candidate_paths:
            continue
        added_modules = []
        removed_modules = set()
        for line in block:
            if not line or line[0] not in "+-" or line.startswith(("+++", "---")):
                continue
            for module in line_modules(line[1:]):
                if not module or len(module.encode("utf-8")) > 4096:
                    continue
                if line[0] == "-":
                    removed_modules.add(module)
                elif module not in added_modules:
                    added_modules.append(module)
        modules = [module for module in added_modules if module not in removed_modules]
        if modules:
            bindings.append({"path": path, "modules": modules})
    return bindings


def _js_suite_load_failure_proof_matches(
    proof,
    log_text,
    expected_command,
    observed_command,
):
    if not expected_command or expected_command != observed_command:
        return False
    bindings = proof.get("suite_module_mocks")
    targets = proof.get("targets")
    if (
        not isinstance(bindings, list)
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
                or not module
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
        or not _js_path_matches(missing.group(2), target_suite)
    ):
        return False
    return re.search(
        r"(?m)^FAIL\s+" + re.escape(bound_suite) + r"\s*$",
        str(log_text or ""),
    ) is not None


def _js_candidate_failure_proof_matches(
    proof,
    log_text,
    expected_command,
    observed_command,
):
    if not expected_command or expected_command != observed_command:
        return False
    targets = proof.get("targets")
    candidate_paths = proof.get("candidate_source_paths")
    if (
        not isinstance(targets, list)
        or not targets
        or not isinstance(candidate_paths, list)
        or not candidate_paths
    ):
        return False
    target_suites = {
        str(target).split(" | ", 1)[0].replace("\\", "/").removeprefix("./")
        for target in targets
    }
    reports = []
    for line in str(log_text or "").splitlines():
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and "numTotalTestSuites" in value:
            reports.append(value)
    if len(reports) != 1 or reports[0].get("success") is not False:
        return False
    results = reports[0].get("testResults")
    if not isinstance(results, list) or not results:
        return False
    messages = [
        str(result.get("message") or "").replace("\\", "/")
        for result in results
        if isinstance(result, dict)
        and result.get("status") == "failed"
        and any(_js_path_matches(result.get("name"), suite) for suite in target_suites)
    ]
    module_bindings = proof.get("candidate_module_bindings")

    def candidate_failure_in(message):
        missing = re.search(
            r"Cannot find module ['\"]([^'\"]+)['\"] from ['\"]([^'\"]+)['\"]",
            message,
        )
        if missing:
            return any(
                isinstance(binding, dict)
                and missing.group(1) in (binding.get("modules") or [])
                and _js_path_matches(binding.get("path"), missing.group(2))
                for binding in module_bindings or []
            )
        return any(path in message for path in candidate_paths) and any(
            marker in message for marker in ("ReferenceError", "SyntaxError", "TypeError")
        )

    return bool(
        messages
        and any(candidate_failure_in(message) for message in messages)
    )




__all__ = [
    "_js_candidate_module_bindings",
    "_js_candidate_failure_proof_matches",
    "_js_path_matches",
    "_js_suite_load_failure_proof_matches",
    "_js_suite_module_mock_bindings",
    "_js_test_patch_files",
]
