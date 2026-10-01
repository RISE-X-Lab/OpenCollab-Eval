"""Source research materials remain available when tests run outside the checkout."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.support.paths import SOURCE_ROOT, TEST_ROOT


def test_experiment_checks_run_from_a_relocated_test_tree(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    shutil.copytree(TEST_ROOT, external / "tests", ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    assert not (external / "experiment").exists()

    environment = os.environ.copy()
    environment["OPENCOLLAB_EVAL_SOURCE_ROOT"] = str(SOURCE_ROOT)
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(external), environment.get("PYTHONPATH", "")) if part
    )
    checks = (
        "tests/experiment/test_batch_launcher.py::test_real_spec_reproduces_the_hand_launched_command",
        "tests/experiment/test_model_fork_audit.py::test_the_shipped_declaration_matches_the_code_it_declares",
        "tests/experiment/test_research_integration.py::test_merged_prediction_selects_the_observed_record_within_one_batch",
        "tests/experiment/test_task_sampling.py::test_no_repository_takes_more_than_the_cap_of_the_suite",
    )
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-c", os.devnull, "--import-mode=importlib", *checks],
        cwd=external,
        env=environment,
        check=True,
    )
