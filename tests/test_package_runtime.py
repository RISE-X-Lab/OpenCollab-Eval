from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from opencollab_eval.commands.package_runtime import package_runtime
from opencollab_eval.commands.swe_v1_prolite_config import verify_runtime_manifest


def test_packaged_runtime_imports_both_packages_from_its_own_source(tmp_path):
    output = tmp_path / "runtime"
    result = package_runtime(output)
    assert result["model_calls"] == 0
    assert verify_runtime_manifest(output) == result["source_tree"]
    assert not (output / ".git").exists()
    assert not list(output.rglob("*.orig"))
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,opencollab,opencollab_eval; print(json.dumps([opencollab.__file__,opencollab_eval.__file__]))",
        ],
        env={**os.environ, "PYTHONPATH": str(output / "src")},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert all(path.startswith(str(output / "src")) for path in json.loads(completed.stdout))


def test_packaging_requires_a_new_destination(tmp_path):
    marker = tmp_path / "existing-data"
    marker.write_text("keep")
    with pytest.raises(FileExistsError):
        package_runtime(tmp_path)
    assert marker.read_text() == "keep"
