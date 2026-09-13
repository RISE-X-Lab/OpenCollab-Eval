"""Candidate-bound pytest collection-failure evidence."""

from __future__ import annotations

import hashlib
import json

from swe_v1_prolite_runner_test_support import _remote_namespace, pytest

from opencollab_eval.commands.swe_rejudge_direct_eval import _read_trusted_test_patch
from opencollab_eval.engine.swe_test_plan_contract import validated_test_plan_kind
from opencollab_eval.engine.swe_v1_pytest_candidate_callsites import (
    pytest_plan_with_test_patch_callsites,
)


def _collection_session(command_sha256: str) -> str:
    raw_events = [
        {"event": "session_start"},
        {"event": "collection_finish", "nodeids": []},
        {"event": "session_finish", "exitstatus": 4},
    ]
    raw = b"".join(
        (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()
        for event in raw_events
    )
    events = [dict(event) for event in raw_events]
    events[0]["controller"] = {
        "schema": "opencollab.pytest_controller.v1",
        "worker_pid": 123,
        "worker_uid": 65534,
        "controller_uid": 0,
        "command_sha256": command_sha256,
    }
    events[-1]["controller"] = {
        "termination": "normal_protocol_eof",
        "worker_returncode": 4,
        "event_stream_sha256": hashlib.sha256(raw).hexdigest(),
    }
    return "".join(json.dumps(event) + "\n" for event in events)


def _test_patch(
    source,
    *,
    marker="+",
    test_file="tests/unit/keyinput/test_keyutils.py",
):
    return (
        f"diff --git a/{test_file} b/{test_file}\n"
        f"--- a/{test_file}\n"
        f"+++ b/{test_file}\n"
        "@@ -500,0 +501,1 @@\n"
        f"{marker}    {source}\n"
    )


def _proof_matches(
    tmp_path,
    candidate_paths,
    source,
    message,
    *,
    test_patch=None,
    target_file="tests/unit/keyinput/test_keyutils.py",
):
    namespace = _remote_namespace(tmp_path)
    target = f"{target_file}::TestKeySequence::test_parse"
    plan = namespace["prolite_test_plan"](
        {
            "repo_language": "python",
            "test_patch": test_patch or "",
        },
        [target],
        candidate_source_paths=candidate_paths,
    )
    proof = plan["proofs"][0]
    command = plan["commands"][0]
    log = (
        f"ERROR collecting {target_file}\n"
        f"{target_file}:503: in TestKeySequence\n"
        f"    {source}\n"
        f"E   TypeError: {message}\n"
    )
    return namespace["_plan_log_failure_proof_matches"](
        proof,
        log,
        _collection_session(proof["command_sha256"]),
        command,
        command,
    )


def test_collection_call_binding_error_is_bound_to_candidate_callsite(tmp_path):
    source = "('x', keyutils.KeySequence(keyutils.KeyInfo(Qt.Key.Key_X))),"
    assert _proof_matches(
        tmp_path,
        ["qutebrowser/keyinput/keyutils.py"],
        source,
        "__init__() missing 1 required positional argument: 'modifiers'",
        test_patch=_test_patch(source),
    ) is True


@pytest.mark.parametrize(
    ("candidate_paths", "source", "message", "patch_source", "marker"),
    [
        (
            ["qutebrowser/keyinput/other.py"],
            "keyutils.KeyInfo(Qt.Key.Key_X)",
            "__init__() missing 1 required positional argument: 'modifiers'",
            "keyutils.KeyInfo(Qt.Key.Key_X)",
            "+",
        ),
        (
            ["qutebrowser/keyinput/keyutils.py"],
            "build_key_info(Qt.Key.Key_X)",
            "__init__() missing 1 required positional argument: 'modifiers'",
            "build_key_info(Qt.Key.Key_X)",
            "+",
        ),
        (
            ["qutebrowser/keyinput/keyutils.py"],
            "keyutils.KeyInfo(Qt.Key.Key_X)",
            "cannot unpack non-iterable NoneType object",
            "keyutils.KeyInfo(Qt.Key.Key_X)",
            "+",
        ),
        (
            ["qutebrowser/keyinput/keyutils.py"],
            "keyutils.build_key_info(Qt.Key.Key_X)",
            "parse_key() missing 1 required positional argument: 'modifiers'",
            "keyutils.build_key_info(Qt.Key.Key_X)",
            "+",
        ),
        (
            ["qutebrowser/keyinput/keyutils.py", "vendor/keyutils.py"],
            "keyutils.KeyInfo(Qt.Key.Key_X)",
            "__init__() missing 1 required positional argument: 'modifiers'",
            "keyutils.KeyInfo(Qt.Key.Key_X)",
            "+",
        ),
        (
            ["qutebrowser/keyinput/keyutils.py"],
            "keyutils.KeyInfo(Qt.Key.Key_X)",
            "__init__() missing 1 required positional argument: 'modifiers'",
            "keyutils.KeyInfo(Qt.Key.Key_Y)",
            "+",
        ),
        (
            ["qutebrowser/keyinput/keyutils.py"],
            "keyutils.KeyInfo(Qt.Key.Key_X)",
            "__init__() missing 1 required positional argument: 'modifiers'",
            "keyutils.KeyInfo(Qt.Key.Key_X)",
            " ",
        ),
        (
            ["qutebrowser/keyinput/keyutils.py"],
            "keyutils.KeyInfo(env.Builder())",
            "__init__() missing 1 required positional argument: 'modifiers'",
            "keyutils.KeyInfo(env.Builder())",
            "+",
        ),
        (
            ["qutebrowser/keyinput/keyutils.py"],
            "keyutils.KeyInfo(Factory())",
            "__init__() missing 1 required positional argument: 'modifiers'",
            "keyutils.KeyInfo(Factory())",
            "+",
        ),
        (
            ["qutebrowser/keyinput/keyutils.py"],
            "keyutils.wrap(other.__init__())",
            "__init__() missing 1 required positional argument: 'modifiers'",
            "keyutils.wrap(other.__init__())",
            "+",
        ),
        (
            ["qutebrowser/keyinput/keyutils.py"],
            "keyutils.KeyInfo(env.KeyInfo())",
            "KeyInfo.__init__() missing 1 required positional argument: 'modifiers'",
            "keyutils.KeyInfo(env.KeyInfo())",
            "+",
        ),
        (
            ["qutebrowser/keyinput/keyutils.py"],
            "keyutils.parse_key(env.parse_key())",
            "parse_key() missing 1 required positional argument: 'modifiers'",
            "keyutils.parse_key(env.parse_key())",
            "+",
        ),
    ],
)
def test_collection_callsite_requires_unambiguous_binding_evidence(
    tmp_path,
    candidate_paths,
    source,
    message,
    patch_source,
    marker,
):
    assert _proof_matches(
        tmp_path,
        candidate_paths,
        source,
        message,
        test_patch=_test_patch(patch_source, marker=marker),
    ) is False


def test_collection_callsite_without_fixed_patch_binding_remains_technical(tmp_path):
    assert _proof_matches(
        tmp_path,
        ["qutebrowser/keyinput/keyutils.py"],
        "keyutils.KeyInfo(Qt.Key.Key_X)",
        "__init__() missing 1 required positional argument: 'modifiers'",
    ) is False


def test_collection_callsite_in_another_test_file_remains_technical(tmp_path):
    source = "keyutils.KeyInfo(Qt.Key.Key_X)"
    assert _proof_matches(
        tmp_path,
        ["qutebrowser/keyinput/keyutils.py"],
        source,
        "__init__() missing 1 required positional argument: 'modifiers'",
        test_patch=_test_patch(source, test_file="tests/unit/other/test_keyutils.py"),
    ) is False


def test_collection_callsite_requires_exact_repository_relative_test_path(tmp_path):
    source = "keyutils.KeyInfo(Qt.Key.Key_X)"
    assert _proof_matches(
        tmp_path,
        ["qutebrowser/keyinput/keyutils.py"],
        source,
        "__init__() missing 1 required positional argument: 'modifiers'",
        test_patch=_test_patch(source, test_file="tests/unit/keyinput/test_keyutils.py"),
        target_file="nested/tests/unit/keyinput/test_keyutils.py",
    ) is False


def test_enrichment_rejects_persisted_callsites_that_disagree_with_fixed_patch(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "tests/unit/keyinput/test_keyutils.py::TestKeySequence::test_parse"
    source = "keyutils.KeyInfo(Qt.Key.Key_X)"
    patch = _test_patch(source)
    plan = namespace["prolite_test_plan"](
        {"repo_language": "python", "test_patch": patch},
        [target],
        candidate_source_paths=["qutebrowser/keyinput/keyutils.py"],
    )
    assert validated_test_plan_kind(plan, require_commands=True) == "pytest"
    plan["proofs"][0]["candidate_test_callsites"][0]["source"] = (
        "other.KeyInfo(Qt.Key.Key_X)"
    )
    assert validated_test_plan_kind(plan, require_commands=True) is None
    with pytest.raises(ValueError, match="disagree with the fixed test patch"):
        pytest_plan_with_test_patch_callsites(plan, patch)


def test_trusted_test_patch_reader_rejects_missing_malformed_and_mismatched_digest(tmp_path):
    with pytest.raises(RuntimeError, match="invalid expected"):
        _read_trusted_test_patch(tmp_path, "short")
    with pytest.raises(FileNotFoundError):
        _read_trusted_test_patch(tmp_path, "0" * 64)
    patch = b"fixed test patch\n"
    (tmp_path / "test.patch").write_bytes(patch)
    with pytest.raises(RuntimeError, match="does not match"):
        _read_trusted_test_patch(tmp_path, "0" * 64)
    digest = hashlib.sha256(patch).hexdigest()
    assert _read_trusted_test_patch(tmp_path, digest) == (patch.decode(), digest)
