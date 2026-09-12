from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

from e2e.deterministic_swe_driver import _synthetic_sources


def test_e2e_fixture_activates_the_actual_named_python_environment(tmp_path):
    context = tmp_path / "image"
    context.mkdir()
    _synthetic_sources(context, "fixture-validation")
    dockerfile = (context / "Dockerfile").read_text()
    setup = next(line[4:] for line in dockerfile.splitlines() if line.startswith("RUN python -m venv"))
    conda = tmp_path / "miniconda3"
    setup = setup.replace("/opt/miniconda3", str(conda))
    activate = conda / "bin/activate"
    command = (
        setup + "\nsource " + shlex.quote(str(activate)) + " testbed\n"
        + 'printf "%s\\n" "$CONDA_DEFAULT_ENV"\n'
        + "python -c 'import sys; print(sys.prefix)'"
    )
    result = subprocess.run(
        ["bash", "-c", command],
        env={**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]},
        text=True, capture_output=True, check=True, timeout=30,
    )
    assert result.stdout.splitlines()[-2:] == ["testbed", str(conda / "envs/testbed")]
