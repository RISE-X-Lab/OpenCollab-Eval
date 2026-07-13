"""Pytest target parsing and structured execution-proof helpers."""

# ruff: noqa: E501, F403, F405

from opencollab_eval.engine.swe_v1_remote_records import *
from opencollab_eval.engine.swe_v1_remote_state import *
from opencollab_eval.engine.swe_v1_remote_target_proof import *


def _pytest_controller_proof_matches(events):
    if not isinstance(events, list) or len(events) < 3:
        return False
    start = events[0]
    finish = events[-1]
    start_controller = start.get("controller") if isinstance(start, dict) else None
    finish_controller = finish.get("controller") if isinstance(finish, dict) else None
    if not isinstance(start_controller, dict) or not isinstance(finish_controller, dict):
        return False
    worker_pid = start_controller.get("worker_pid")
    worker_uid = start_controller.get("worker_uid")
    controller_uid = start_controller.get("controller_uid")
    returncode = finish_controller.get("worker_returncode")
    if (
        start_controller.get("schema") != "opencollab.pytest_controller.v1"
        or isinstance(worker_pid, bool)
        or not isinstance(worker_pid, int)
        or worker_pid <= 0
        or isinstance(worker_uid, bool)
        or not isinstance(worker_uid, int)
        or isinstance(controller_uid, bool)
        or not isinstance(controller_uid, int)
        or worker_uid == controller_uid
        or re.fullmatch(r"[0-9a-f]{64}", str(start_controller.get("command_sha256") or "")) is None
        or finish_controller.get("termination") != "normal_protocol_eof"
        or isinstance(returncode, bool)
        or not isinstance(returncode, int)
        or finish.get("exitstatus") != returncode
    ):
        return False
    raw_events = []
    for index, event in enumerate(events):
        raw_event = dict(event)
        if index in {0, len(events) - 1}:
            raw_event.pop("controller", None)
        raw_events.append(raw_event)
    raw_payload = b"".join(
        (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        for event in raw_events
    )
    return finish_controller.get("event_stream_sha256") == hashlib.sha256(raw_payload).hexdigest()


def _pytest_target_matches_node(target, node):
    if "::" in target:
        return node == target or node.startswith(target + "[")
    prefix = target.rstrip("/")
    return node == prefix or node.startswith(prefix + "::") or node.startswith(prefix + "/")


def _pytest_parameter_parent(target):
    target = str(target or "")
    node_start = target.rfind("::") + 2
    bracket = target.find("[", node_start)
    if node_start < 2 or bracket <= node_start or not target.endswith("]"):
        return ""
    return target[:bracket]


def _pytest_fallback_parents_match_targets(targets, fallback_parents):
    expected = []
    for target in targets:
        parent = _pytest_parameter_parent(target)
        if parent and parent not in expected:
            expected.append(parent)
    return (
        isinstance(fallback_parents, list)
        and fallback_parents == expected
        and all(isinstance(parent, str) and parent for parent in fallback_parents)
    )


def _pytest_structured_proof_matches(
    targets,
    proof_text,
    log_text,
    fallback_parents=None,
):
    try:
        events = [json.loads(line) for line in proof_text.splitlines() if line.strip()]
    except json.JSONDecodeError:
        return False
    if (
        not events
        or any(not isinstance(event, dict) for event in events)
        or not _pytest_controller_proof_matches(events)
    ):
        return False
    if events[0].get("event") != "session_start" or events[-1].get("event") != "session_finish":
        return False
    if sum(event.get("event") == "session_start" for event in events) != 1:
        return False
    collections = [event for event in events if event.get("event") == "collection_finish"]
    finishes = [event for event in events if event.get("event") == "session_finish"]
    if len(collections) != 1 or len(finishes) != 1 or finishes[0].get("exitstatus") != 0:
        return False
    nodeids = collections[0].get("nodeids")
    if (
        not isinstance(nodeids, list)
        or not nodeids
        or len(set(nodeids)) != len(nodeids)
        or any(not isinstance(node, str) or not node for node in nodeids)
    ):
        return False
    fallback_parents = fallback_parents or []
    if fallback_parents and not _pytest_fallback_parents_match_targets(
        targets, fallback_parents
    ):
        return False
    exact_targets = (
        [target for target in targets if not _pytest_parameter_parent(target)]
        if fallback_parents
        else list(targets)
    )
    allowed_targets = [*exact_targets, *fallback_parents]
    if any(
        not any(_pytest_target_matches_node(target, node) for target in allowed_targets)
        for node in nodeids
    ):
        return False
    reports = {}
    for event in events:
        if event.get("event") != "runtest_logreport":
            continue
        node = event.get("nodeid")
        phase = event.get("when")
        outcome = event.get("outcome")
        if (
            not isinstance(node, str)
            or node not in nodeids
            or phase not in {"setup", "call", "teardown"}
            or outcome not in {"passed", "failed", "skipped"}
            or phase in reports.get(node, {})
        ):
            return False
        reports.setdefault(node, {})[phase] = outcome
    for target in exact_targets:
        matching = [node for node in nodeids if _pytest_target_matches_node(target, node)]
        if not matching or any(
            reports.get(node)
            != {"setup": "passed", "call": "passed", "teardown": "passed"}
            for node in matching
        ):
            return False
    for parent in fallback_parents:
        matching = [node for node in nodeids if node.startswith(parent + "[")]
        if not matching or any(
            reports.get(node)
            != {"setup": "passed", "call": "passed", "teardown": "passed"}
            for node in matching
        ):
            return False
    if any(
        line.strip().startswith(("FAILED ", "ERROR "))
        for line in log_text.splitlines()
    ) or re.search(r"\bno tests (?:ran|collected)\b", log_text, flags=re.IGNORECASE):
        return False
    return True


def _pytest_structured_failure_proof_matches(
    targets,
    proof_text,
    fallback_parents=None,
):
    try:
        events = [json.loads(line) for line in proof_text.splitlines() if line.strip()]
    except json.JSONDecodeError:
        return False
    if (
        not events
        or any(not isinstance(event, dict) for event in events)
        or not _pytest_controller_proof_matches(events)
    ):
        return False
    starts = [event for event in events if event.get("event") == "session_start"]
    collections = [event for event in events if event.get("event") == "collection_finish"]
    finishes = [event for event in events if event.get("event") == "session_finish"]
    if len(starts) != 1 or len(collections) != 1 or len(finishes) != 1:
        return False
    if finishes[0].get("exitstatus") in {None, 0}:
        return False
    nodeids = collections[0].get("nodeids")
    if not isinstance(nodeids, list) or not nodeids or any(
        not isinstance(node, str) or not node for node in nodeids
    ):
        return False
    fallback_parents = fallback_parents or []
    if fallback_parents and not _pytest_fallback_parents_match_targets(
        targets, fallback_parents
    ):
        return False
    exact_targets = (
        [target for target in targets if not _pytest_parameter_parent(target)]
        if fallback_parents
        else list(targets)
    )
    allowed_targets = [*exact_targets, *fallback_parents]
    if fallback_parents and any(
        not any(_pytest_target_matches_node(target, node) for target in allowed_targets)
        for node in nodeids
    ):
        return False
    return any(
        event.get("event") == "runtest_logreport"
        and event.get("nodeid") in nodeids
        and event.get("when") in {"setup", "call", "teardown"}
        and event.get("outcome") == "failed"
        and any(
            _pytest_target_matches_node(target, event["nodeid"])
            for target in allowed_targets
        )
        for event in events
    )


_PYTHON_TEST_ROOTS = {
    "spec",
    "specs",
    "test",
    "tests",
}
_PYTHON_SOURCE_LAYOUT_ROOTS = {
    "application",
    "applications",
    "apps",
    "lib",
    "package",
    "packages",
    "src",
}


def _python_repo_module_roots(repo, target_file):
    roots = set()
    slug = str(repo or "").rsplit("/", 1)[-1].replace("-", "_").lower()
    if re.fullmatch(r"[a-z_][a-z0-9_]*", slug):
        roots.add(slug)
    parts = [
        part.replace("-", "_").lower()
        for part in pathlib.PurePosixPath(str(target_file or "")).parts[:-1]
    ]
    candidate = ""
    if parts and parts[0] in _PYTHON_TEST_ROOTS:
        candidate = ""
    elif len(parts) >= 2 and parts[0] in _PYTHON_SOURCE_LAYOUT_ROOTS:
        candidate = parts[1]
    elif parts:
        candidate = parts[0]
    if re.fullmatch(r"[a-z_][a-z0-9_]*", candidate):
        roots.add(candidate)
    return roots


def _python_module_is_repo_local(module, repo, target_file):
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", str(module or "")):
        return False
    return module.split(".", 1)[0].lower() in _python_repo_module_roots(
        repo, target_file
    )


def _python_test_patch_import_bindings(row, targets):
    target_files = {
        str(target).split("::", 1)[0].replace("\\", "/").removeprefix("./")
        for target in targets
    }
    if len(target_files) != 1:
        return []
    target_file = next(iter(target_files))
    bindings = []
    for block in split_patch_blocks(str(row.get("test_patch") or "")):
        path = diff_target_path(block[0] if block else "")
        normalized_path = path.replace("\\", "/").removeprefix("./")
        if not (
            normalized_path == target_file
            or normalized_path.endswith("/" + target_file)
            or target_file.endswith("/" + normalized_path)
        ):
            continue
        modules = []
        for line in block:
            if not line.startswith("+") or line.startswith("+++"):
                continue
            try:
                tree = ast.parse(line[1:].strip())
            except (IndentationError, SyntaxError):
                continue
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    candidates = [node.module]
                elif isinstance(node, ast.Import):
                    candidates = [alias.name for alias in node.names]
                else:
                    candidates = []
                for module in candidates:
                    if (
                        _python_module_is_repo_local(
                            module,
                            row.get("repo"),
                            normalized_path,
                        )
                        and module not in modules
                    ):
                        modules.append(module)
        if modules:
            bindings.append({"test_file": normalized_path, "modules": modules})
    return bindings


def _pytest_bound_import_failure_matches(
    target_file,
    target_imports,
    repo,
    log_text,
):
    if not isinstance(target_imports, list) or not target_imports or len(target_imports) > 64:
        return False
    matched_modules = []
    for binding in target_imports:
        if not isinstance(binding, dict):
            return False
        path = str(binding.get("test_file") or "").replace("\\", "/").removeprefix("./")
        modules = binding.get("modules")
        if path != target_file and not path.endswith("/" + target_file):
            continue
        if (
            matched_modules
            or not isinstance(modules, list)
            or not modules
            or len(modules) > 128
            or len(set(modules)) != len(modules)
            or any(
                not isinstance(module, str)
                or len(module.encode("utf-8")) > 4096
                or not _python_module_is_repo_local(module, repo, path)
                for module in modules
            )
        ):
            return False
        matched_modules = modules
    if not matched_modules:
        return False
    missing_modules = set(
        re.findall(
            r"(?m)^E\s+(?:ImportError|ModuleNotFoundError):\s+"
            r"No module named ['\"]([^'\"]+)['\"]\s*$",
            log_text,
        )
    )
    if len(missing_modules) != 1:
        return False
    missing = next(iter(missing_modules))
    if missing not in matched_modules:
        return False
    target_frame = re.escape(target_file) + r":[0-9]+: in <module>"
    import_line = (
        r"\n\s*(?:from\s+" + re.escape(missing) + r"\s+import\b|"
        r"import\s+" + re.escape(missing) + r"(?:\s|$))"
    )
    return re.search(r"(?m)^(?:.*?/)?" + target_frame + import_line, log_text) is not None


def _pytest_collection_failure_proof_matches(
    targets,
    proof_text,
    log_text,
    expected_command,
    observed_command,
    candidate_source_paths=None,
    target_imports=None,
    repo="",
):
    if not expected_command or expected_command != observed_command:
        return False
    try:
        events = [json.loads(line) for line in proof_text.splitlines() if line.strip()]
    except json.JSONDecodeError:
        return False
    if (
        len(events) != 3
        or any(not isinstance(event, dict) for event in events)
        or not _pytest_controller_proof_matches(events)
        or [event.get("event") for event in events]
        != ["session_start", "collection_finish", "session_finish"]
        or events[1].get("nodeids") != []
        or isinstance(events[2].get("exitstatus"), bool)
        or not isinstance(events[2].get("exitstatus"), int)
        or events[2]["exitstatus"] == 0
    ):
        return False
    target_files = []
    for target in targets:
        path = target.split("::", 1)[0].replace("\\", "/").removeprefix("./")
        pure = pathlib.PurePosixPath(path)
        if (
            not path.endswith(".py")
            or pure.is_absolute()
            or ".." in pure.parts
            or "\x00" in path
        ):
            return False
        if path not in target_files:
            target_files.append(path)
    if not target_files:
        return False
    collected_paths = re.findall(
        r"(?m)^\s*_*\s*ERROR collecting (\S+?)(?:\s+_+)?\s*$",
        str(log_text or ""),
    )
    normalized_collected = {
        path.replace("\\", "/").removeprefix("./") for path in collected_paths
    }
    if normalized_collected != set(target_files):
        return False
    log_text = str(log_text or "")
    if len(target_files) == 1 and _pytest_bound_import_failure_matches(
        target_files[0],
        target_imports,
        repo,
        log_text,
    ):
        return True
    if (
        not isinstance(candidate_source_paths, list)
        or not candidate_source_paths
        or len(candidate_source_paths) > 1024
        or len(set(candidate_source_paths)) != len(candidate_source_paths)
        or sum(len(str(path).encode("utf-8")) for path in candidate_source_paths)
        > 128 * 1024
        or any(
            not isinstance(path, str)
            or not path.endswith(".py")
            or pathlib.PurePosixPath(path).is_absolute()
            or ".." in pathlib.PurePosixPath(path).parts
            or "\x00" in path
            or is_eval_test_path(path)
            for path in candidate_source_paths
        )
    ):
        return False

    def traceback_has(path):
        return re.search(
            r"(?m)^(?:.*?/)?" + re.escape(path) + r":[0-9]+(?::|$)",
            log_text,
        ) is not None

    semantic_exception = re.search(
        r"(?m)^E\s+(?:AssertionError|AttributeError|KeyError|NameError|"
        r"NotImplementedError|RuntimeError|TypeError|ValueError)(?::|$)",
        log_text,
    )
    return bool(
        semantic_exception
        and any(traceback_has(path) for path in target_files)
        and any(traceback_has(path) for path in candidate_source_paths)
    )



def _bounded_command_batches(items, command_prefix, max_args=80, max_chars=24000):
    """Split exact targets across commands without broadening their meaning."""
    batches = []
    current = []
    for item in items:
        candidate = [*current, item]
        candidate_command = command_prefix + " ".join(shlex.quote(value) for value in candidate)
        if current and (len(candidate) > max_args or len(candidate_command) > max_chars):
            batches.append(current)
            current = [item]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def python_test_target_batches(tests, selected, max_args=80, max_chars=24000):
    targets = [str(item) for item in (tests or selected) if str(item)]
    return _bounded_command_batches(
        targets,
        "pytest -p opencollab_pytest_proof -q -rA -o addopts= ",
        max_args=max_args,
        max_chars=max_chars,
    )


def python_parameter_fallback_batches(tests, max_args=80, max_chars=24000):
    declared_batches = []
    execution_batches = []
    current_declared = []
    current_execution = []
    for target in tests:
        execution_target = _pytest_parameter_parent(target) or target
        candidate_execution = list(current_execution)
        if execution_target not in candidate_execution:
            candidate_execution.append(execution_target)
        candidate_command = "pytest -p opencollab_pytest_proof -q -rA -o addopts= " + " ".join(
            shlex.quote(value) for value in candidate_execution
        )
        if current_declared and (
            len(candidate_execution) > max_args or len(candidate_command) > max_chars
        ):
            declared_batches.append(current_declared)
            execution_batches.append(current_execution)
            current_declared = []
            current_execution = [execution_target]
        else:
            current_execution = candidate_execution
        current_declared.append(target)
    if current_declared:
        declared_batches.append(current_declared)
        execution_batches.append(current_execution)
    fallback_batches = [
        [target for target in execution if any(_pytest_parameter_parent(value) == target for value in declared)]
        for declared, execution in zip(declared_batches, execution_batches, strict=True)
    ]
    return declared_batches, execution_batches, fallback_batches


def compact_python_test_targets(tests, selected, max_args=80, max_chars=24000):
    """Normalize truncated parameter selectors and retain each exact target once."""
    compacted = []
    for raw in tests or selected:
        target = normalize_python_test_target(str(raw))
        if target and target not in compacted:
            compacted.append(target)
    return compacted



def prolite_pytest_proof_plugin_source():
    """Return the worker plugin that streams events to the trusted controller."""
    return r'''import json
import os

_fd = int(os.environ.pop("OPENCOLLAB_PYTEST_EVENT_FD"))
_payload_bytes = 0
_MAX_PROOF_BYTES = 8 * 1024 * 1024


def _emit(event):
    global _payload_bytes
    payload = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if _payload_bytes + len(payload) > _MAX_PROOF_BYTES:
        raise OSError("pytest proof exceeds the bounded size")
    view = memoryview(payload)
    while view:
        written = os.write(_fd, view)
        if written <= 0:
            raise OSError("pytest event write made no progress")
        view = view[written:]
    _payload_bytes += len(payload)


def pytest_sessionstart(session):
    _emit({"event": "session_start"})


def pytest_collection_finish(session):
    _emit({"event": "collection_finish", "nodeids": [item.nodeid for item in session.items]})


def pytest_runtest_logreport(report):
    _emit({"event": "runtest_logreport", "nodeid": report.nodeid, "when": report.when, "outcome": report.outcome})


def pytest_sessionfinish(session, exitstatus):
    _emit({"event": "session_finish", "exitstatus": exitstatus})
'''




__all__ = [
    "_PYTHON_SOURCE_LAYOUT_ROOTS",
    "_PYTHON_TEST_ROOTS",
    "_bounded_command_batches",
    "_pytest_bound_import_failure_matches",
    "_pytest_collection_failure_proof_matches",
    "_pytest_fallback_parents_match_targets",
    "_pytest_parameter_parent",
    "_pytest_structured_failure_proof_matches",
    "_pytest_structured_proof_matches",
    "_pytest_target_matches_node",
    "_python_module_is_repo_local",
    "_python_repo_module_roots",
    "_python_test_patch_import_bindings",
    "compact_python_test_targets",
    "prolite_pytest_proof_plugin_source",
    "python_parameter_fallback_batches",
    "python_test_target_batches",
]
