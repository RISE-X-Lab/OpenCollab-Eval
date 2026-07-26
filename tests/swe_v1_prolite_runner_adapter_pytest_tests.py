"""Pytest plan and external proof-boundary coverage."""

from __future__ import annotations

import hashlib
import json

from swe_v1_prolite_runner_test_support import _remote_namespace, pytest


def _proof_text(events, *, returncode: int, command_sha256: str) -> str:
    raw = "".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
        for event in events
    ).encode()
    events = [dict(event) for event in events]
    events[0]["controller"] = {
        "schema": "opencollab.pytest_controller.v1",
        "worker_pid": 123,
        "worker_uid": 65534,
        "controller_uid": 0,
        "command_sha256": command_sha256,
    }
    events[-1]["controller"] = {
        "termination": "normal_protocol_eof",
        "worker_returncode": returncode,
        "event_stream_sha256": hashlib.sha256(raw).hexdigest(),
    }
    return "".join(json.dumps(event) + "\n" for event in events)


def _session(nodeids, *, command_sha256: str, exitstatus: int = 0, outcome: str = "passed"):
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
                    "outcome": outcome if phase == "call" else "passed",
                }
            )
    events.append({"event": "session_finish", "exitstatus": exitstatus})
    return _proof_text(events, returncode=exitstatus, command_sha256=command_sha256)


def test_pytest_plan_is_exact_and_command_bound(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "tests/test_widget.py::test_widget"

    plan = namespace["prolite_test_plan"]({"repo_language": "python"}, [target])

    assert plan["adapter"] == "pytest"
    assert plan["coverage"] == "exact_targets"
    assert plan["coverage_verified"] is True
    assert plan["declared_targets"] == [target]
    assert plan["target_batches"] == [[target]]
    assert plan["commands"] == [
        "pytest -p opencollab_pytest_proof -q -rA -o addopts= " + target
    ]
    expected_sha = hashlib.sha256(
        "\0".join(plan["commands"][0].split()).encode()
    ).hexdigest()
    assert plan["proofs"][0]["command_sha256"] == expected_sha


def test_missing_language_go_node_uses_go_adapter(tmp_path):
    namespace = _remote_namespace(tmp_path)

    plan = namespace["prolite_test_plan"]({}, ["pkg/widget_test.go::TestWidget"])

    assert plan["adapter"] == "go-test-json"
    assert plan["commands"] == [
        "go test -count=1 -json ./pkg -run '^TestWidget$'"
    ]
    assert plan["proofs"][0]["test"] == "TestWidget"


def test_qutebrowser_plan_keeps_xvfb_and_parameter_parent_fallback(tmp_path):
    namespace = _remote_namespace(tmp_path)
    parent = "tests/unit/test_keys.py::test_key"
    targets = [parent + "[dataset-a]", parent + "[dataset-b]"]

    plan = namespace["prolite_test_plan"](
        {"repo_language": "python", "repo": "qutebrowser/qutebrowser"},
        targets,
    )

    assert plan["coverage"] == "parameter_parent_targets"
    assert plan["commands"] == [
        "xvfb-run -a python -m pytest --no-xvfb "
        "-p opencollab_pytest_proof -q -rA -o addopts= "
        + parent
    ]
    assert plan["proofs"][0]["parameter_fallback_parents"] == [parent]


def test_mixed_exact_and_parameter_targets_validate_across_batches(tmp_path):
    namespace = _remote_namespace(tmp_path)
    exact = [f"tests/test_many.py::test_exact_{index}" for index in range(80)]
    parameter = "tests/test_many.py::test_parameter[dataset]"

    plan = namespace["prolite_test_plan"](
        {"repo_language": "python"}, [*exact, parameter]
    )
    script = namespace["prolite_test_plan_script"](plan, "f2p", "nonce")

    assert plan["coverage"] == "parameter_parent_targets"
    assert plan["coverage_verified"] is True
    assert len(plan["target_batches"]) == 2
    assert "parameter_fallback_parents" not in plan["proofs"][0]
    assert plan["proofs"][1]["parameter_fallback_parents"] == [
        "tests/test_many.py::test_parameter"
    ]
    assert "untrusted test plan is unsupported" not in script
    assert script.count("opencollab_pytest_controller.py") == 2


def test_pytest_script_delegates_to_read_only_controller(tmp_path):
    namespace = _remote_namespace(tmp_path)
    plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        ["tests/test_widget.py::test_widget"],
        candidate_source_paths=["src/widget.py"],
    )

    script = namespace["prolite_test_plan_script"](plan, "f2p", "nonce")

    assert "/eval_input/opencollab_pytest_controller.py" in script
    assert "--command-sha256" in script
    assert plan["proofs"][0]["command_sha256"] in script
    assert "--candidate-source-path" in script
    assert "src/widget.py" in script
    assert "/eval_output/f2p.batch_001.proof.nonce.jsonl" in script


def test_controller_reserves_proof_before_candidate_execution(tmp_path):
    namespace = _remote_namespace(tmp_path)
    controller = {"__name__": "controller_test"}
    exec(namespace["prolite_pytest_controller_source"](), controller)
    output = tmp_path / "output"
    output.mkdir()
    output.chmod(0o1777)
    proof = output / "proof.jsonl"

    identity = controller["_prepare_output"](proof, output)

    assert identity == (proof.stat().st_dev, proof.stat().st_ino, proof.stat().st_uid)
    assert proof.stat().st_mode & 0o777 == 0o600
    assert controller["WORKER_UID"] != 65534
    with pytest.raises(FileExistsError):
        controller["_prepare_output"](proof, output)


def test_eval_output_uses_local_container_directory_for_root_squashed_nfs(tmp_path):
    namespace = _remote_namespace(tmp_path)
    plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        ["tests/test_widget.py::test_widget"],
    )
    reports = tmp_path / "reports"
    output = reports / "task"
    reports.mkdir()

    namespace["prepare_eval_output_directory"](reports, output, "task")
    names = namespace["expected_eval_output_names"](
        plan, {"commands": [], "proofs": []}, "nonce"
    )

    assert "f2p.batch_001.proof.nonce.jsonl" in names
    assert output.stat().st_mode & 0o777 == 0o755


def test_pytest_plan_contract_rejects_command_or_digest_drift(tmp_path):
    namespace = _remote_namespace(tmp_path)
    plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        ["tests/test_widget.py::test_widget"],
    )
    changed_command = {**plan, "commands": [plan["commands"][0] + " tests/test_other.py"]}
    changed_digest = {
        **plan,
        "proofs": [{**plan["proofs"][0], "command_sha256": "a" * 64}],
    }

    for changed in (changed_command, changed_digest):
        script = namespace["prolite_test_plan_script"](changed, "f2p", "nonce")
        assert "untrusted test plan is unsupported" in script
        assert script.endswith("exit 86\n")


def test_pytest_pass_requires_exact_complete_structured_evidence(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "tests/test_widget.py::test_widget"
    plan = namespace["prolite_test_plan"]({"repo_language": "python"}, [target])
    proof = plan["proofs"][0]
    valid = _session([target], command_sha256=proof["command_sha256"])

    assert namespace["_plan_log_proof_matches"](proof, "1 passed", valid) is True
    assert namespace["_plan_log_proof_matches"](
        proof,
        "1 passed",
        _session([target], command_sha256="a" * 64),
    ) is False


def test_pytest_pass_accepts_application_error_logging_with_complete_proof(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "tests/test_widget.py::test_widget"
    plan = namespace["prolite_test_plan"]({"repo_language": "python"}, [target])
    proof = plan["proofs"][0]

    assert namespace["_plan_log_proof_matches"](
        proof,
        "ERROR widget: expected failure was handled\n1 passed",
        _session([target], command_sha256=proof["command_sha256"]),
    ) is True
    assert namespace["_plan_log_proof_matches"](
        proof,
        "no tests ran",
        _session([], command_sha256=proof["command_sha256"], exitstatus=5),
    ) is False


def test_parameter_parent_proof_rejects_unrelated_collection(tmp_path):
    namespace = _remote_namespace(tmp_path)
    parent = "tests/test_many.py::test_case"
    targets = [parent + "[declared-a]", parent + "[declared-b]"]
    plan = namespace["prolite_test_plan"]({"repo_language": "python"}, targets)
    proof = plan["proofs"][0]
    runtime_nodes = [parent + "[runtime-a]", parent + "[runtime-b]"]

    assert namespace["_plan_log_proof_matches"](
        proof,
        "2 passed",
        _session(runtime_nodes, command_sha256=proof["command_sha256"]),
    ) is True
    assert namespace["_plan_log_proof_matches"](
        proof,
        "3 passed",
        _session(
            [*runtime_nodes, "tests/test_other.py::test_case[runtime]"],
            command_sha256=proof["command_sha256"],
        ),
    ) is False


def test_pytest_failure_and_collection_import_failure_are_proven(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target_file = "openlibrary/plugins/worksearch/schemes/tests/test_works.py"
    target = target_file + "::test_process_user_query"
    module = "openlibrary.plugins.worksearch.schemes.works"
    test_patch = (
        f"diff --git a/{target_file} b/{target_file}\n"
        f"--- a/{target_file}\n+++ b/{target_file}\n@@ -0,0 +1 @@\n"
        f"+from {module} import WorkSearchScheme\n"
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
    command = plan["commands"][0]
    failed = _session(
        [target],
        command_sha256=proof["command_sha256"],
        exitstatus=1,
        outcome="failed",
    )
    collection = _session(
        [], command_sha256=proof["command_sha256"], exitstatus=4
    )
    import_log = (
        f"ERROR collecting {target_file}\n"
        f"{target_file}:2: in <module>\n"
        f"    from {module} import WorkSearchScheme\n"
        f"E   ModuleNotFoundError: No module named '{module}'\n"
    )

    assert namespace["_plan_log_failure_proof_matches"](
        proof, "1 failed", failed, command, command
    ) is True
    assert namespace["_plan_log_failure_proof_matches"](
        proof, import_log, collection, command, command
    ) is True
    assert namespace["_plan_log_failure_proof_matches"](
        proof, import_log, collection, command, command + " changed"
    ) is False


def test_collection_attribute_error_is_bound_to_candidate_module(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "tests/unit/config/test_configfiles.py::test_version_change_filter"
    candidate = "qutebrowser/config/configfiles.py"
    plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        [target],
        candidate_source_paths=[candidate],
    )
    proof = plan["proofs"][0]
    command = plan["commands"][0]
    collection = _session([], command_sha256=proof["command_sha256"], exitstatus=4)
    log = (
        "ERROR collecting tests/unit/config/test_configfiles.py\n"
        "tests/unit/config/test_configfiles.py:180: in <module>\n"
        "E   AttributeError: module 'qutebrowser.config.configfiles' has no attribute 'VersionChange'\n"
    )

    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, collection, command, command
    ) is True
    proof["candidate_source_paths"] = ["qutebrowser/config/other.py"]
    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, collection, command, command
    ) is False

    proof["candidate_source_paths"] = [
        "docs/examples/qutebrowser/config/configfiles.py"
    ]
    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, collection, command, command
    ) is False


def test_collection_import_error_is_bound_only_to_exact_candidate_module(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "tests/unit/config/test_configfiles.py::test_version_change_filter"
    candidate = "qutebrowser/config/configfiles.py"
    plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        [target],
        candidate_source_paths=[candidate],
    )
    proof = plan["proofs"][0]
    command = plan["commands"][0]
    collection = _session([], command_sha256=proof["command_sha256"], exitstatus=4)
    log = (
        "ERROR collecting tests/unit/config/test_configfiles.py\n"
        "tests/unit/config/test_configfiles.py:180: in <module>\n"
        "E   ImportError: cannot import name 'VersionChange' from "
        "'qutebrowser.config.configfiles' (/app/qutebrowser/config/configfiles.py)\n"
    )

    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, collection, command, command
    ) is True
    for root in ("src", "lib", "package"):
        proof["candidate_source_paths"] = [
            f"{root}/qutebrowser/config/configfiles.py"
        ]
        assert namespace["_plan_log_failure_proof_matches"](
            proof, log, collection, command, command
        ) is True
    proof["candidate_source_paths"] = ["third_party/qutebrowser/config/configfiles.py"]
    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, collection, command, command
    ) is False


def test_collection_module_shortcut_rejects_ambiguous_module_errors(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "tests/unit/config/test_configfiles.py::test_version_change_filter"
    plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        [target],
        candidate_source_paths=[
            "qutebrowser/config/configfiles.py",
            "qutebrowser/config/configtypes.py",
        ],
    )
    proof = plan["proofs"][0]
    command = plan["commands"][0]
    collection = _session([], command_sha256=proof["command_sha256"], exitstatus=4)
    log = (
        "ERROR collecting tests/unit/config/test_configfiles.py\n"
        "tests/unit/config/test_configfiles.py:180: in <module>\n"
        "E   AttributeError: module 'qutebrowser.config.configfiles' has no attribute 'VersionChange'\n"
        "E   AttributeError: module 'qutebrowser.config.configtypes' has no attribute 'ChangeFilter'\n"
    )

    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, collection, command, command
    ) is False


@pytest.mark.parametrize(
    "error_line",
    [
        "AttributeError: partially initialized module 'qutebrowser.config.configfiles' "
        "has no attribute 'VersionChange'",
        "ImportError: cannot import name 'VersionChange' from partially initialized module "
        "'qutebrowser.config.configfiles'",
    ],
)
def test_collection_partial_initialization_is_bound_to_exact_candidate(
    tmp_path,
    error_line,
):
    namespace = _remote_namespace(tmp_path)
    target = "tests/unit/config/test_configfiles.py::test_version_change_filter"
    plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        [target],
        candidate_source_paths=["qutebrowser/config/configfiles.py"],
    )
    proof = plan["proofs"][0]
    command = plan["commands"][0]
    collection = _session([], command_sha256=proof["command_sha256"], exitstatus=4)
    log = (
        "ERROR collecting tests/unit/config/test_configfiles.py\n"
        "tests/unit/config/test_configfiles.py:180: in <module>\n"
        f"E   {error_line}\n"
    )

    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, collection, command, command
    ) is True
    proof["candidate_source_paths"] = ["qutebrowser/config/other.py"]
    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, collection, command, command
    ) is False


def test_python_empty_and_unsupported_targets_remain_non_runnable(tmp_path):
    namespace = _remote_namespace(tmp_path)
    command = namespace["prolite_test_command"]
    is_runnable = namespace["_is_runnable_test_command"]

    assert command({"repo_language": "python"}, []) == ""
    assert command({"repo_language": "ruby"}, ["spec/widget_spec.rb"]) == ""
    assert not is_runnable("")
    assert not is_runnable("true")
    assert not is_runnable("pytest target")


@pytest.mark.parametrize("status", [0, 1, 4, 5, 86])
def test_uncontrolled_legacy_pytest_events_never_prove_execution(tmp_path, status):
    namespace = _remote_namespace(tmp_path)
    target = "tests/test_widget.py::test_widget"
    plan = namespace["prolite_test_plan"]({"repo_language": "python"}, [target])
    proof = plan["proofs"][0]
    raw = "\n".join(
        json.dumps(event)
        for event in (
            {"event": "session_start"},
            {"event": "collection_finish", "nodeids": [target]},
            {"event": "session_finish", "exitstatus": status},
        )
    )

    assert namespace["_plan_log_proof_matches"](proof, "1 passed", raw) is False
