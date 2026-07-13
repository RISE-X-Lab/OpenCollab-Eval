"""Pytest adapter execution-proof tests."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from swe_v1_prolite_runner_test_support import _remote_namespace, pytest

from opencollab_eval.engine.swe_v1_remote_pytest_controller import (
    prolite_pytest_controller_source,
)


def _controller_proof(events, *, returncode):
    raw = "".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
        for event in events
    ).encode()
    events[0]["controller"] = {
        "schema": "opencollab.pytest_controller.v1",
        "worker_pid": 123,
        "worker_uid": 65534,
        "controller_uid": 0,
        "command_sha256": "a" * 64,
    }
    events[-1]["controller"] = {
        "termination": "normal_protocol_eof",
        "worker_returncode": returncode,
        "event_stream_sha256": hashlib.sha256(raw).hexdigest(),
    }
    return "".join(json.dumps(event) + "\n" for event in events)


def _pytest_proof_text(nodeids, *, exitstatus=0, call_outcome="passed"):
    events = [
        {"event": "session_start"},
        {"event": "collection_finish", "nodeids": list(nodeids)},
    ]
    for nodeid in nodeids:
        for phase in ("setup", "call", "teardown"):
            events.append(
                {
                    "event": "runtest_logreport",
                    "nodeid": nodeid,
                    "when": phase,
                    "outcome": call_outcome if phase == "call" else "passed",
                }
            )
    events.append({"event": "session_finish", "exitstatus": exitstatus})
    return _controller_proof(events, returncode=exitstatus)


def _run_pytest_worker(command, *, cwd, plugin_dir):
    read_fd, write_fd = os.pipe()
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "PYTHONPATH": str(plugin_dir),
                "OPENCOLLAB_PYTEST_EVENT_FD": str(write_fd),
            },
            pass_fds=(write_fd,),
        )
    finally:
        os.close(write_fd)
    with os.fdopen(read_fd, encoding="utf-8") as handle:
        raw_proof = handle.read()
    events = [json.loads(line) for line in raw_proof.splitlines()]
    proof_text = _controller_proof(events, returncode=result.returncode)
    return result, proof_text



def test_prolite_test_command_never_falls_back_to_a_passing_noop(tmp_path):
    namespace = _remote_namespace(tmp_path)
    command = namespace["prolite_test_command"]
    is_runnable = namespace["_is_runnable_test_command"]

    assert command({"repo_language": "python"}, []) == ""
    assert command({}, []) == ""
    assert command({"repo_language": "ruby"}, ["spec/widget_spec.rb"]) == ""
    assert command(
        {"repo_language": "ruby", "test_cmd": "echo ok", "eval_cmd": "echo also-ok"},
        ["spec/widget_spec.rb"],
    ) == ""
    assert not is_runnable("")
    assert not is_runnable("true")
    assert not is_runnable(" : ")
    assert not is_runnable("echo ok")
    assert is_runnable(
        "pytest -p opencollab_pytest_proof -q -rA -o addopts= "
        "tests/test_x.py::test_y"
    )


def test_prolite_pytest_console_ignores_shadow_module_and_collect_only_addopts(
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    target = "test_target.py::test_target"
    plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        [target],
    )
    (tmp_path / "pytest.py").write_text(
        "print('shadow pytest: no tests')\n",
        encoding="utf-8",
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\naddopts = --collect-only\n",
        encoding="utf-8",
    )
    (tmp_path / "test_target.py").write_text(
        "def test_target():\n    assert True\n",
        encoding="utf-8",
    )
    plugin_dir = tmp_path / "proof-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "opencollab_pytest_proof.py").write_text(
        namespace["prolite_pytest_proof_plugin_source"](),
        encoding="utf-8",
    )
    command = shlex.split(plan["commands"][0])
    command[0] = str(Path(sys.executable).with_name("pytest"))
    result, proof_text = _run_pytest_worker(command, cwd=tmp_path, plugin_dir=plugin_dir)
    log = result.stdout + result.stderr

    assert result.returncode == 0
    assert "shadow pytest" not in log
    assert namespace["_plan_log_proof_matches"](
        plan["proofs"][0],
        log,
        proof_text,
    ) is True


def test_prolite_pytest_proof_rejects_conftest_exit_status_rewrite(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "test_target.py::test_target"
    plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        [target],
    )
    (tmp_path / "test_target.py").write_text(
        "def test_target():\n    assert False\n",
        encoding="utf-8",
    )
    (tmp_path / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n"
        f"    print('\\nPASSED {target}')\n"
        "    session.exitstatus = 0\n",
        encoding="utf-8",
    )
    plugin_dir = tmp_path / "proof-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "opencollab_pytest_proof.py").write_text(
        namespace["prolite_pytest_proof_plugin_source"](),
        encoding="utf-8",
    )
    command = shlex.split(plan["commands"][0])
    command[0] = str(Path(sys.executable).with_name("pytest"))
    result, proof_text = _run_pytest_worker(command, cwd=tmp_path, plugin_dir=plugin_dir)
    log = result.stdout + result.stderr

    assert result.returncode == 0
    assert f"PASSED {target}" in log
    assert f"FAILED {target}" in log
    assert namespace["_plan_log_proof_matches"](
        plan["proofs"][0],
        log,
        proof_text,
    ) is False


def test_prolite_pytest_proof_rejects_cleared_collection_with_forged_pass(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "test_target.py::test_target"
    plan = namespace["prolite_test_plan"]({"repo_language": "python"}, [target])
    (tmp_path / "test_target.py").write_text(
        "def test_target():\n    assert True\n",
        encoding="utf-8",
    )
    (tmp_path / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    items.clear()\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        f"    print('\\nPASSED {target}')\n"
        "    session.exitstatus = 0\n",
        encoding="utf-8",
    )
    plugin_dir = tmp_path / "proof-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "opencollab_pytest_proof.py").write_text(
        namespace["prolite_pytest_proof_plugin_source"](),
        encoding="utf-8",
    )
    command = shlex.split(plan["commands"][0])
    command[0] = str(Path(sys.executable).with_name("pytest"))

    result, proof_text = _run_pytest_worker(command, cwd=tmp_path, plugin_dir=plugin_dir)
    log = result.stdout + result.stderr

    assert result.returncode == 0
    assert "no tests ran" in log
    assert namespace["_plan_log_proof_matches"](
        plan["proofs"][0],
        log,
        proof_text,
    ) is False


def test_prolite_pytest_proof_rejects_forged_pass_line_and_summary(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "test_target.py::test_target"
    proof = {"kind": "pytest_structured_reports", "targets": [target]}
    forged_failure = (
        f"PASSED {target}\n"
        f"FAILED {target} - AssertionError\n"
        "1 failed in 0.01s\n"
        "1 passed in 0.01s\n"
    )
    forged_empty = f"PASSED {target}\n1 passed in 0.01s\nno tests ran in 0.00s\n"

    structured = _pytest_proof_text([target])
    assert namespace["_plan_log_proof_matches"](proof, forged_failure, structured) is False
    assert namespace["_plan_log_proof_matches"](proof, forged_empty, structured) is False


def test_prolite_pytest_proof_rejects_legacy_or_tampered_worker_events(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "test_target.py::test_target"
    proof = {"kind": "pytest_structured_reports", "targets": [target]}
    legacy = [
        {"event": "session_start"},
        {"event": "collection_finish", "nodeids": [target]},
        *[
            {
                "event": "runtest_logreport",
                "nodeid": target,
                "when": phase,
                "outcome": "passed",
            }
            for phase in ("setup", "call", "teardown")
        ],
        {"event": "session_finish", "exitstatus": 0},
    ]
    legacy_text = "".join(json.dumps(event) + "\n" for event in legacy)
    controlled = _controller_proof(legacy, returncode=0)
    tampered = controlled.replace('"outcome": "passed"', '"outcome": "failed"', 1)

    assert namespace["_plan_log_proof_matches"](proof, "1 passed", legacy_text) is False
    assert namespace["_plan_log_proof_matches"](proof, "1 passed", tampered) is False


@pytest.mark.parametrize(
    ("source", "expected_status"),
    [
        ("def test_target():\n    assert True\n", 0),
        ("def test_target():\n    assert False\n", 1),
        ("import opencollab_missing_production_module\n", 4),
    ],
)
def test_prolite_pytest_proof_is_host_readable_after_session_finish(
    tmp_path,
    source,
    expected_status,
):
    namespace = _remote_namespace(tmp_path)
    (tmp_path / "test_target.py").write_text(source, encoding="utf-8")
    plugin_dir = tmp_path / "proof-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "opencollab_pytest_proof.py").write_text(
        namespace["prolite_pytest_proof_plugin_source"](),
        encoding="utf-8",
    )
    result, proof_text = _run_pytest_worker(
        [
            str(Path(sys.executable).with_name("pytest")),
            "-p",
            "opencollab_pytest_proof",
            "-q",
            "-o",
            "addopts=",
            "test_target.py::test_target",
        ],
        cwd=tmp_path,
        plugin_dir=plugin_dir,
    )

    assert result.returncode == expected_status
    events = [json.loads(line) for line in proof_text.splitlines()]
    assert events[-1]["event"] == "session_finish"
    assert events[-1]["exitstatus"] == expected_status
    assert events[-1]["controller"]["worker_returncode"] == expected_status


@pytest.mark.parametrize("existing_kind", ["regular", "symlink"])
def test_prolite_pytest_proof_keeps_exclusive_nofollow_creation(
    tmp_path,
    existing_kind,
):
    source = prolite_pytest_controller_source()
    victim = tmp_path / "victim.jsonl"
    victim.write_text("sentinel\n", encoding="utf-8")
    proof_path = tmp_path / "proof.jsonl"
    if existing_kind == "regular":
        proof_path.write_text("existing\n", encoding="utf-8")
    else:
        proof_path.symlink_to(victim)
    controller = {"__name__": "controller_test"}
    exec(source, controller)
    with pytest.raises(FileExistsError):
        controller["_publish"](
            proof_path,
            [{"event": "session_start"}, {"event": "session_finish", "exitstatus": 0}],
            {"worker_pid": 123, "command_sha256": "a" * 64, "returncode": 0},
        )

    assert victim.read_text(encoding="utf-8") == "sentinel\n"
    if existing_kind == "regular":
        assert proof_path.read_text(encoding="utf-8") == "existing\n"


def test_prolite_pytest_worker_streams_events_without_a_proof_path(
    tmp_path,
    monkeypatch,
):
    namespace = _remote_namespace(tmp_path)
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv("OPENCOLLAB_PYTEST_EVENT_FD", str(write_fd))
    plugin = {}
    exec(namespace["prolite_pytest_proof_plugin_source"](), plugin)

    plugin["pytest_sessionstart"](None)
    plugin["pytest_sessionfinish"](None, 0)
    os.close(write_fd)
    with os.fdopen(read_fd, encoding="utf-8") as handle:
        events = [json.loads(line) for line in handle]

    assert "OPENCOLLAB_PYTEST_EVENT_FD" not in os.environ
    assert events == [
        {"event": "session_start"},
        {"event": "session_finish", "exitstatus": 0},
    ]


def test_prolite_pytest_proof_rejects_candidate_rewrite_followed_by_clean_exit(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "test_target.py::test_target"
    plan = namespace["prolite_test_plan"]({"repo_language": "python"}, [target])
    (tmp_path / "test_target.py").write_text(
        "import json\n"
        "import os\n\n"
        "def test_target():\n"
        "    path = os.environ.get('OPENCOLLAB_PYTEST_PROOF_PATH')\n"
        "    if path:\n"
        "        events = [\n"
        "            {'event': 'session_start'},\n"
        f"            {{'event': 'collection_finish', 'nodeids': ['{target}']}},\n"
        f"            {{'event': 'runtest_logreport', 'nodeid': '{target}', 'when': 'call', 'outcome': 'passed'}},\n"
        "            {'event': 'session_finish', 'exitstatus': 0},\n"
        "        ]\n"
        "        with open(path, 'w', encoding='utf-8') as handle:\n"
        "            handle.write('\\n'.join(json.dumps(item) for item in events) + '\\n')\n"
        "    os._exit(0)\n",
        encoding="utf-8",
    )
    script = namespace["prolite_test_plan_script"](plan, "f2p", "nonce")
    controller = prolite_pytest_controller_source()

    assert "opencollab_pytest_controller.py" in script
    assert "OPENCOLLAB_PYTEST_PROOF_PATH" not in script
    assert "os.setgroups([])" in controller
    assert "os.setuid(WORKER_UID)" in controller
    assert "pytest worker protocol is incomplete" in controller
    assert "OPENCOLLAB_PYTEST_PROOF_PATH" not in namespace["prolite_pytest_proof_plugin_source"]()


def test_prolite_pytest_collection_import_failure_is_exact_semantic_failure(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target_file = "openlibrary/plugins/worksearch/schemes/tests/test_works.py"
    target = target_file + "::test_process_user_query"
    module = "openlibrary.plugins.worksearch.schemes.works"
    test_patch = (
        f"diff --git a/{target_file} b/{target_file}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{target_file}\n"
        "@@ -0,0 +1,2 @@\n"
        f"+from {module} import WorkSearchScheme\n"
        "+def test_process_user_query(): pass\n"
    )
    plan = namespace["prolite_test_plan"](
        {
            "repo_language": "python",
            "repo": "internetarchive/openlibrary",
            "test_patch": test_patch,
        },
        [target],
    )
    proof = plan["proofs"][0]
    proof_text = _pytest_proof_text([], exitstatus=4)
    command = plan["commands"][0]
    valid_log = (
        f"ERROR collecting {target_file}\n"
        f"{target_file}:2: in <module>\n"
        f"    from {module} import WorkSearchScheme\n"
        f"E   ModuleNotFoundError: No module named '{module}'\n"
    )

    assert proof["target_imports"] == [
        {"test_file": target_file, "modules": [module]}
    ]
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        valid_log,
        proof_text,
        command,
        command,
    ) is True
    invalid_logs = (
        "no tests ran in 0.01s\n",
        valid_log.replace(module, "openlibrary.plugins.unbound", 1),
        valid_log.replace("ERROR collecting " + target_file, "ERROR collecting tests/test_other.py"),
        valid_log.replace("from " + module, "from openlibrary.plugins.unbound"),
    )
    for log in invalid_logs:
        assert namespace["_plan_log_failure_proof_matches"](
            proof,
            log,
            proof_text,
            command,
            command,
        ) is False
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        valid_log,
        proof_text,
        command,
        command + " # changed",
    ) is False


@pytest.mark.parametrize(
    "target_file",
    [
        "tests/numpy/test_target.py",
        "test/numpy/test_target.py",
        "spec/numpy/test_target.py",
        "specs/numpy/test_target.py",
    ],
)
def test_prolite_pytest_collection_rejects_unbound_third_party_import(
    tmp_path,
    target_file,
):
    namespace = _remote_namespace(tmp_path)
    target = target_file + "::test_target"
    test_patch = (
        f"diff --git a/{target_file} b/{target_file}\n"
        f"--- a/{target_file}\n"
        f"+++ b/{target_file}\n"
        "@@ -0,0 +1 @@\n"
        "+import numpy\n"
    )
    plan = namespace["prolite_test_plan"](
        {
            "repo_language": "python",
            "repo": "example/repo",
            "test_patch": test_patch,
        },
        [target],
        candidate_source_paths=["src/candidate.py"],
    )
    proof = plan["proofs"][0]
    proof_text = _pytest_proof_text([], exitstatus=4)
    log = (
        f"ERROR collecting {target_file}\n"
        f"{target_file}:1: in <module>\n"
        "    import numpy\n"
        "E   ModuleNotFoundError: No module named 'numpy'\n"
    )

    assert "target_imports" not in proof
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        log,
        proof_text,
        plan["commands"][0],
        plan["commands"][0],
    ) is False


def test_python_repo_module_roots_respect_test_and_source_layouts(tmp_path):
    namespace = _remote_namespace(tmp_path)

    assert namespace["_python_repo_module_roots"](
        "example/repo", "tests/numpy/test_target.py"
    ) == {"repo"}
    assert namespace["_python_repo_module_roots"](
        "example/repo", "src/localpkg/tests/test_target.py"
    ) == {"repo", "localpkg"}
    assert namespace["_python_repo_module_roots"](
        "internetarchive/openlibrary",
        "openlibrary/plugins/worksearch/schemes/tests/test_works.py",
    ) == {"openlibrary"}


def test_prolite_pytest_collection_candidate_exception_binds_modified_source(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "tests/unit/keyinput/test_keyutils.py::test_target[param]"
    source_path = "qutebrowser/keyinput/keyutils.py"
    proof = {
        "kind": "pytest_structured_reports",
        "targets": [target],
        "parameter_fallback_parents": [target.split("[", 1)[0]],
        "candidate_source_paths": [source_path],
    }
    proof_text = _pytest_proof_text([], exitstatus=4)
    command = (
        "pytest -p opencollab_pytest_proof -q -rA -o addopts= "
        "tests/unit/keyinput/test_keyutils.py::test_target"
    )
    valid_log = (
        "ERROR collecting tests/unit/keyinput/test_keyutils.py\n"
        "tests/unit/keyinput/test_keyutils.py:247: in <module>\n"
        f"{source_path}:501: in _convert_key\n"
        "E   AssertionError: <Ctrl+Alt+y>\n"
    )

    assert namespace["_plan_log_failure_proof_matches"](
        proof, valid_log, proof_text, command, command
    ) is True
    rejected_logs = (
        valid_log.replace("tests/unit/keyinput/test_keyutils.py:247", "tests/unit/other.py:247"),
        valid_log.replace(f"{source_path}:501", "qutebrowser/other.py:501"),
        valid_log.replace("AssertionError", "ConnectionError"),
        valid_log.replace(
            "ERROR collecting tests/unit/keyinput/test_keyutils.py",
            "ERROR collecting tests/unit/other.py",
        ),
    )
    for log in rejected_logs:
        assert namespace["_plan_log_failure_proof_matches"](
            proof, log, proof_text, command, command
        ) is False
    assert namespace["_plan_log_failure_proof_matches"](
        proof, valid_log, proof_text, command, command + " changed"
    ) is False


def test_prolite_pytest_collection_candidate_exception_covers_every_target_file(tmp_path):
    namespace = _remote_namespace(tmp_path)
    targets = [
        "tests/unit/keyinput/test_bindingtrie.py::test_target[param]",
        "tests/unit/keyinput/test_keyutils.py::test_other[param]",
    ]
    source_path = "qutebrowser/keyinput/keyutils.py"
    proof = {
        "kind": "pytest_structured_reports",
        "targets": targets,
        "candidate_source_paths": [source_path],
    }
    proof_text = _pytest_proof_text([], exitstatus=4)
    command = "exact multi-file pytest command"
    valid_log = (
        "ERROR collecting tests/unit/keyinput/test_bindingtrie.py\n"
        "tests/unit/keyinput/test_bindingtrie.py:32: in <module>\n"
        "ERROR collecting tests/unit/keyinput/test_keyutils.py\n"
        "tests/unit/keyinput/test_keyutils.py:502: in TestKeySequence\n"
        f"{source_path}:501: in _convert_key\n"
        "E   AssertionError: <Ctrl+Alt+y>\n"
    )

    assert namespace["_plan_log_failure_proof_matches"](
        proof, valid_log, proof_text, command, command
    ) is True
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        valid_log.replace(
            "ERROR collecting tests/unit/keyinput/test_bindingtrie.py\n",
            "",
        ),
        proof_text,
        command,
        command,
    ) is False
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        valid_log + "ERROR collecting tests/unit/keyinput/test_unrelated.py\n",
        proof_text,
        command,
        command,
    ) is False


def test_prolite_pytest_parameter_parent_fallback_proves_all_collected_instances(tmp_path):
    namespace = _remote_namespace(tmp_path)
    parent = "tests/test_many.py::test_case"
    targets = [parent + "[dataset-a]", parent + "[dataset-b]"]
    proof = {
        "kind": "pytest_structured_reports",
        "targets": targets,
        "parameter_fallback_parents": [parent],
    }
    actual_nodes = [parent + "[runtime-repr-1]", parent + "[runtime-repr-2]"]
    complete = _pytest_proof_text(actual_nodes)

    assert namespace["_plan_log_proof_matches"](proof, "2 passed", complete) is True
    assert namespace["_plan_log_proof_matches"](
        proof,
        "1 passed",
        _pytest_proof_text(actual_nodes[:1]),
    ) is True
    assert namespace["_plan_log_proof_matches"](
        proof,
        "2 passed",
        _pytest_proof_text([*actual_nodes, "tests/other.py::test_case[runtime]"]),
    ) is False
    assert namespace["_plan_log_proof_matches"](
        proof,
        "1 failed",
        _pytest_proof_text(actual_nodes, call_outcome="failed"),
    ) is False
    assert namespace["_plan_log_proof_matches"](
        proof,
        "no tests ran in 0.01s",
        _pytest_proof_text([], exitstatus=5),
    ) is False


def test_prolite_model_patch_filters_pytest_conftest_changes(tmp_path):
    namespace = _remote_namespace(tmp_path)
    patch = (
        "diff --git a/conftest.py b/conftest.py\n"
        "--- a/conftest.py\n"
        "+++ b/conftest.py\n"
        "@@ -0,0 +1 @@\n"
        "+def pytest_sessionfinish(session): session.exitstatus = 0\n"
        "diff --git a/src/widget.py b/src/widget.py\n"
        "--- a/src/widget.py\n"
        "+++ b/src/widget.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    filtered = namespace["filter_model_patch_for_eval"](patch)

    assert "conftest.py" not in filtered
    assert "src/widget.py" in filtered
