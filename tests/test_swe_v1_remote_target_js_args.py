from __future__ import annotations

import json
import os
import shlex
import subprocess

import pytest
from swe_v1_prolite_runner_test_support import _command_namespace


@pytest.mark.parametrize("runner_prefix", ["pnpm test", "corepack pnpm test"])
@pytest.mark.parametrize(
    ("extra_args", "target", "expected_suffix"),
    [
        ("", "", ""),
        ("--json", "test/example.test.js", " -- --json test/example.test.js"),
        (
            "--config jest.config.js",
            "test/example.test.js",
            " -- --config jest.config.js test/example.test.js",
        ),
    ],
)
def test_js_runner_pnpm_fallback_passes_script_args_after_separator(
    runner_prefix, extra_args, target, expected_suffix
):
    namespace = _command_namespace()
    command = namespace["js_runner_command"]("jest", "test", target, extra_args)

    assert f"{runner_prefix}{expected_suffix}" in command
    assert "---" not in command


def test_js_runner_quotes_space_and_metacharacter_arguments(tmp_path):
    namespace = _command_namespace()
    marker = tmp_path / "must-not-be-created"
    target_value = f"test/space;$(touch {marker}).js"
    extra_value = "--grep " + shlex.quote(f"suite; $(touch {marker})")
    command = namespace["js_runner_command"](
        "jest", "test", shlex.quote(target_value), extra_value
    )

    binary = tmp_path / "node_modules/.bin/jest"
    binary.parent.mkdir(parents=True)
    arguments_file = tmp_path / "arguments.json"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        "python3 -c 'import json, os, sys; "
        "json.dump(sys.argv[1:], open(os.environ[\"JS_ARGS\"], \"w\"))' \"$@\"\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=tmp_path,
        env={**os.environ, "JS_ARGS": str(arguments_file)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(arguments_file.read_text(encoding="utf-8")) == [
        "--grep",
        f"suite; $(touch {marker})",
        target_value,
    ]
    assert not marker.exists()
