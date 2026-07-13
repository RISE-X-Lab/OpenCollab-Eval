from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

_TEST_CHECKOUT_ROOT = Path(__file__).resolve().parents[1]
_FIELDS = {"owner", "status", "implementation", "test", "nodeid"}
_STATUSES = {"fixed", "partial", "deferred"}


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        assert key not in result, f"duplicate JSON key: {key}"
        result[key] = value
    return result


def _eval_root() -> Path:
    configured = os.environ.get("OPENCOLLAB_EVAL_SOURCE_ROOT")
    candidate = Path(configured).expanduser() if configured else _TEST_CHECKOUT_ROOT
    root = candidate.resolve()
    if not (root / "docs" / "integrity-coverage.json").is_file():
        raise AssertionError("OpenCollab-Eval source checkout is required to validate integrity coverage")
    return root


def _opencollab_root() -> Path:
    configured = os.environ.get("OPENCOLLAB_SOURCE_ROOT")
    candidates = (
        Path(configured).expanduser() if configured else None,
        _eval_root().parent / "OpenCollab",
        _eval_root().parent / "opencollab-source",
    )
    for candidate in candidates:
        if candidate and (candidate / "opencollab" / "pyproject.toml").is_file():
            return candidate.resolve()
    raise AssertionError("OpenCollab source checkout is required to validate cross-repository coverage")


def _load() -> dict[str, dict[str, str]]:
    ledger = _eval_root() / "docs" / "integrity-coverage.json"
    data = json.loads(ledger.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    assert data["schema_version"] == 1
    fields = data["fields"]
    assert fields == ["owner", "status", "implementation", "test", "nodeid"]
    coverage = {}
    for control_id, values in data["coverage"].items():
        assert len(values) == len(fields), control_id
        coverage[control_id] = dict(zip(fields, values, strict=True))
    return coverage


def _platforms() -> dict[str, str]:
    ledger = _eval_root() / "docs" / "integrity-coverage.json"
    data = json.loads(ledger.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    return data["platforms"]


def _roots() -> dict[str, tuple[Path, Path]]:
    eval_root = _eval_root()
    oc_root = _opencollab_root()
    return {
        "OpenCollab-Eval": (eval_root, eval_root),
        "OpenCollab": (oc_root, oc_root / "opencollab"),
    }


def _collection_environment(pytest_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    test_support = str(pytest_root / "tests")
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (test_support, existing)))
    environment["PATH"] = os.pathsep.join(
        filter(None, (str(Path(sys.executable).parent), environment.get("PATH")))
    )
    return environment


def test_integrity_coverage_ledger_is_complete_and_truthful() -> None:
    coverage = _load()
    platforms = _platforms()
    expected_ids = [f"H-{index:02d}" for index in range(1, 72)]
    assert list(coverage) == expected_ids
    assert platforms == {"H-68": "darwin"}
    assert set(platforms) <= set(coverage)
    roots = _roots()
    for control_id, record in coverage.items():
        assert set(record) == _FIELDS, control_id
        assert all(isinstance(value, str) and value for value in record.values()), control_id
        assert record["owner"] in roots, control_id
        assert record["status"] in _STATUSES, control_id
        owner_root, _pytest_root = roots[record["owner"]]
        for field in ("implementation", "test"):
            relative = Path(record[field])
            assert not relative.is_absolute() and ".." not in relative.parts, control_id
            assert (owner_root / relative).is_file(), f"{control_id}: missing {field} path {relative}"
        nodeid_path = record["nodeid"].partition("::")[0]
        expected_nodeid_path = (
            Path(record["test"]).relative_to("opencollab").as_posix()
            if record["owner"] == "OpenCollab"
            else record["test"]
        )
        assert nodeid_path == expected_nodeid_path, control_id
    assert coverage["H-07"]["status"] == "deferred"
    assert coverage["H-69"]["status"] == "partial"
    assert coverage["H-71"]["nodeid"].endswith(
        "test_run_tests_rejects_go_multi_selector_before_any_command"
    )


def test_integrity_coverage_nodeids_execute_successfully() -> None:
    grouped: dict[str, set[str]] = defaultdict(set)
    platforms = _platforms()
    for control_id, record in _load().items():
        if control_id in platforms and platforms[control_id] != sys.platform:
            continue
        grouped[record["owner"]].add(record["nodeid"])
    roots = _roots()
    for owner, nodeids in grouped.items():
        _owner_root, pytest_root = roots[owner]
        ordered = sorted(nodeids)
        with tempfile.TemporaryDirectory() as temporary_directory:
            junit_path = Path(temporary_directory) / "integrity-results.xml"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    f"--junitxml={junit_path}",
                    *ordered,
                ],
                cwd=pytest_root,
                env=_collection_environment(pytest_root),
                text=True,
                capture_output=True,
                timeout=300,
                check=False,
            )
            assert junit_path.is_file(), f"{owner} did not produce executable test evidence"
            summary = ET.parse(junit_path).getroot()
            suites = list(summary.findall("testsuite")) if summary.tag == "testsuites" else [summary]
            executed = sum(int(suite.attrib["tests"]) for suite in suites)
            skipped = sum(int(suite.attrib["skipped"]) for suite in suites)
            assert executed == len(ordered), f"{owner} integrity test count mismatch"
            assert skipped == 0, f"{owner} integrity tests were skipped"
        assert result.returncode == 0, f"{owner} integrity tests failed:\n{result.stdout}\n{result.stderr}"


def test_platform_specific_integrity_controls_have_ci_probes() -> None:
    eval_workflow = (_eval_root() / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    opencollab_root, _pytest_root = _roots()["OpenCollab"]
    owner_workflow = (opencollab_root / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    h68 = _load()["H-68"]

    assert h68["owner"] == "OpenCollab"
    assert eval_workflow.count("repository: YihongDong/OpenCollab") == 1
    assert "KaiEureka/OpenCollab" not in eval_workflow
    assert "runs-on: macos-latest" not in eval_workflow
    assert h68["nodeid"] not in eval_workflow
    assert "runs-on: macos-latest" in owner_workflow
    assert h68["nodeid"] in owner_workflow
    assert '--junitxml="$RUNNER_TEMP/h68.xml"' in owner_workflow
    assert '"skipped": 0' in owner_workflow
