from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def test_real_claude_runtime_cannot_read_host_sentinel_or_rewrite_control(
    tmp_path: Path,
) -> None:
    runtime_image = os.environ.get("OPENCOLLAB_TEST_CLAUDE_RUNTIME_IMAGE")
    runtime_image_id = os.environ.get("OPENCOLLAB_TEST_CLAUDE_RUNTIME_IMAGE_ID")
    if not runtime_image or not runtime_image_id:
        pytest.skip("Claude runtime probe is not configured")
    inspected = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", runtime_image],
        text=True,
        capture_output=True,
        check=False,
    )
    if inspected.returncode != 0:
        pytest.skip("configured Claude runtime image is unavailable")
    if inspected.stdout.strip() != runtime_image_id:
        pytest.fail("configured Claude runtime image identity does not match")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    helper = tmp_path / "run_in_container"
    helper.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    helper.chmod(0o700)
    settings = tmp_path / "settings.json"
    settings.write_text("{}\n", encoding="utf-8")
    sentinel = tmp_path / "host-secret-sentinel"
    sentinel.write_text("must stay on host\n", encoding="utf-8")
    probe = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--mount",
            f"type=bind,src={workspace},dst=/workspace",
            "--mount",
            f"type=bind,src={helper},dst=/control/run_in_container,readonly",
            "--mount",
            f"type=bind,src={settings},dst=/control/settings.json,readonly",
            "--entrypoint",
            "bash",
            runtime_image,
            "-lc",
            (
                'test ! -e "$1" && test ! -e /output && '
                "! (printf x >> /control/run_in_container) 2>/dev/null && "
                "! (printf x >> /control/settings.json) 2>/dev/null && "
                'printf "isolated=true\\n"'
            ),
            "--",
            str(sentinel),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "isolated=true"
    assert helper.read_text() == "#!/usr/bin/env bash\nexit 0\n"
    assert settings.read_text() == "{}\n"
