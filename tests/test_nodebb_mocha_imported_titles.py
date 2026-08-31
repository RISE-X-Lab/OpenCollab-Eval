from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from opencollab_eval.engine.swe_test_plan_contract import validated_test_plan_kind
from opencollab_eval.engine.swe_v1_remote_target_proof import (
    fail_to_pass_execution_proof,
    mocha_test_command,
)
from opencollab_eval.engine.swe_v1_remote_test_plan import prolite_test_plan


def _install_recording_mocha(tmp_path: Path) -> Path:
    binary = tmp_path / "node_modules" / ".bin" / "mocha"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['MOCHA_CALLS']).write_text(json.dumps(sys.argv[1:]))\n"
        "print('[\"end\",{\"tests\":2,\"passes\":2,\"failures\":0}]')\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def test_nodebb_target_file_selector_removes_imported_suite_markers(tmp_path: Path):
    _install_recording_mocha(tmp_path)
    calls = tmp_path / "mocha-call.json"
    target_file = tmp_path / "targets.json"
    targets = [
        "test/database.js | Test database test/database/sorted.js::Sorted Set methods "
        "test/database/sorted.js::getSortedSetsMembers should return members with scores",
        "test/database.js | Test database test/database/sorted.js::Sorted Set methods "
        "test/database/sorted.js::getSortedSetsMembers should return multiple members with scores",
    ]
    target_file.write_text(json.dumps(targets), encoding="utf-8")

    result = subprocess.run(
        mocha_test_command(targets, ["test/database.js"], str(target_file)),
        shell=True,
        cwd=tmp_path,
        env={**os.environ, "MOCHA_CALLS": str(calls)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    arguments = json.loads(calls.read_text(encoding="utf-8"))
    selector = arguments[arguments.index("--grep") + 1]
    assert re.fullmatch(
        selector,
        "Test database Sorted Set methods getSortedSetsMembers should return members with scores",
    )
    assert "test/database/sorted.js::" not in selector


def test_nodebb_target_file_selector_preserves_significant_trailing_space(tmp_path: Path):
    _install_recording_mocha(tmp_path)
    calls = tmp_path / "mocha-call.json"
    target_file = tmp_path / "targets.json"
    target = (
        "test/database.js | Test database test/database/sorted.js::Sorted Set methods "
        "test/database/sorted.js::getSortedSetRange() should work with big arrays "
        "(length > 100) "
    )
    target_file.write_text(json.dumps([target]), encoding="utf-8")

    result = subprocess.run(
        mocha_test_command([target], ["test/database.js"], str(target_file)),
        shell=True,
        cwd=tmp_path,
        env={**os.environ, "MOCHA_CALLS": str(calls)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    arguments = json.loads(calls.read_text(encoding="utf-8"))
    selector = arguments[arguments.index("--grep") + 1]
    runtime_title = (
        "Test database Sorted Set methods getSortedSetRange() should work with big arrays "
        "(length > 100) "
    )
    assert re.fullmatch(selector, runtime_title)
    assert re.fullmatch(selector, runtime_title.rstrip()) is None


def test_nodebb_target_file_falls_back_when_local_mocha_is_not_executable(
    tmp_path: Path,
):
    """A present but non-executable shim must not mask a usable yarn runner."""

    local_mocha = tmp_path / "node_modules" / ".bin" / "mocha"
    local_mocha.parent.mkdir(parents=True)
    local_mocha.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' local-mocha-was-used > \"$LOCAL_MOCHA_CALLED\"\n"
        "exit 97\n",
        encoding="utf-8",
    )
    local_mocha.chmod(0o644)

    yarn = tmp_path / "bin" / "yarn"
    yarn.parent.mkdir()
    yarn.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" > \"$YARN_CALLS\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    yarn.chmod(0o755)

    target_file = tmp_path / "targets.json"
    target_file.write_text(json.dumps(["test/a.js | works"]), encoding="utf-8")
    calls = tmp_path / "yarn-call.txt"
    local_called = tmp_path / "local-call.txt"
    result = subprocess.run(
        mocha_test_command(
            ["test/a.js | works"],
            ["test/a.js"],
            str(target_file),
        ),
        shell=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{yarn.parent}:{os.environ['PATH']}",
            "YARN_CALLS": str(calls),
            "LOCAL_MOCHA_CALLED": str(local_called),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text(encoding="utf-8").startswith("test ")
    assert not local_called.exists()


def test_nodebb_structured_proof_maps_runtime_titles_to_imported_targets():
    expected = [
        "test/database.js | Test database should work",
        "test/database.js | Test database test/database/sorted.js::Sorted Set methods "
        "test/database/sorted.js::getSortedSetsMembers should return members with scores",
    ]
    log = "\n".join(
        [
            '["pass",{"fullTitle":"Test database should work"}]',
            '["pass",{"fullTitle":"Test database Sorted Set methods '
            'getSortedSetsMembers should return members with scores"}]',
        ]
    )

    proof = fail_to_pass_execution_proof(
        {"repo_language": "js", "repo": "nodebb/nodebb"}, expected, 0, log
    )

    assert proof["ok"] is True
    assert proof["observed"] == expected
    assert proof["passed"] == expected
    assert proof["missing"] == []


def test_nodebb_import_normalization_collision_is_rejected():
    targets = [
        "test/database.js | Test database test/database/a.js::Suite should work",
        "test/database.js | Test database test/database/b.js::Suite should work",
    ]
    plan = prolite_test_plan(
        {
            "repo_language": "js",
            "repo": "nodebb/nodebb",
            "selected_test_files_to_run": ["test/database.js"],
        },
        targets,
        target_file="/eval_input/f2p.targets.json",
    )

    assert plan["commands"] == []
    assert plan["coverage_verified"] is False
    assert validated_test_plan_kind(plan, require_commands=True) is None
    proof = fail_to_pass_execution_proof(
        {"repo_language": "js", "repo": "nodebb/nodebb"},
        targets,
        0,
        '["pass",{"fullTitle":"Test database Suite should work"}]',
    )
    assert proof["observed"] == []
    assert proof["missing"] == targets


def test_nodebb_cross_file_duplicate_titles_are_bound_by_file():
    targets = ["test/a.js | same title", "test/b.js | same title"]
    row = {
        "repo_language": "js",
        "repo": "nodebb/nodebb",
        "selected_test_files_to_run": ["test/a.js", "test/b.js"],
    }
    plan = prolite_test_plan(
        row,
        targets,
        target_file="/eval_input/f2p.targets.json",
    )

    assert plan["commands"]
    assert plan["coverage_verified"] is True
    log = "\n".join(
        [
            'OPENCOLLAB_MOCHA_FILE "test/a.js"',
            '["pass",{"fullTitle":"same title"}]',
            'OPENCOLLAB_MOCHA_FILE "test/b.js"',
            '["pass",{"fullTitle":"same title"}]',
        ]
    )
    proof = fail_to_pass_execution_proof(row, targets, 0, log)
    assert proof["ok"] is True
    assert proof["passed"] == targets
