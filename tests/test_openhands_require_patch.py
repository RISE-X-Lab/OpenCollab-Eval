from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from package_test_support import module_path

_SWEBENCH_DIR = module_path("opencollab_eval.generation.gen_prediction").parent
if str(_SWEBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_SWEBENCH_DIR))

from opencollab_eval.generation import openhands_require_patch as guard  # noqa: E402


def test_stop_guard_runs_as_copied_standalone_script(tmp_path: Path) -> None:
    script = tmp_path / "openhands_require_patch.py"
    script.write_text(Path(guard.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    env = os.environ.copy()
    env.pop("OPENHANDS_CONTAINER_ID", None)
    package_root = module_path("opencollab_eval").parent.parent
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(package_root), current_pythonpath) if value
    )

    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "decision": "allow",
        "reason": "missing_container_id",
    }


def _env(tmp_path: Path, *, rejections: int = 2) -> dict[str, str]:
    return {
        "OPENHANDS_CONTAINER_ID": "container-123",
        "OPENHANDS_WORKSPACE": "/testbed",
        "OPENHANDS_OUTPUT_DIR": str(tmp_path),
        "OPENHANDS_EMPTY_PATCH_REJECTIONS": str(rejections),
    }


def test_stop_guard_allows_non_test_source_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        guard,
        "_container_patch",
        lambda *args: "diff --git a/app/main.py b/app/main.py\n+changed\n",
    )

    result = guard.evaluate_stop(_env(tmp_path))

    assert result["decision"] == "allow"
    assert result["reason"] == "source_patch_present"


def test_stop_guard_rejects_empty_or_test_only_patch_then_allows_at_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        guard,
        "_container_patch",
        lambda *args: "diff --git a/tests/test_app.py b/tests/test_app.py\n+changed\n",
    )
    env = _env(tmp_path, rejections=2)

    first = guard.evaluate_stop(env)
    second = guard.evaluate_stop(env)
    third = guard.evaluate_stop(env)

    assert first["decision"] == "deny"
    assert second["decision"] == "deny"
    assert "Only validation/test files changed" in first["additionalContext"]
    assert third == {
        "decision": "allow",
        "reason": "empty_patch_rejection_limit_reached",
    }
    state = json.loads((tmp_path / "empty_patch_stop_guard.json").read_text())
    assert state["rejections"] == 2
    assert state["exhausted"] is True


def test_stop_guard_rejects_yarn_install_state_as_generated_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        guard,
        "_container_patch",
        lambda *args: (
            "diff --git a/.yarn/install-state.gz b/.yarn/install-state.gz\n"
            "new file mode 100644\n"
            "Binary files /dev/null and b/.yarn/install-state.gz differ\n"
        ),
    )

    result = guard.evaluate_stop(_env(tmp_path, rejections=1))

    assert result["decision"] == "deny"
    assert result["reason"] == "empty_source_patch"
    assert "Only generated files changed: .yarn/install-state.gz" in result["additionalContext"]
    state = json.loads((tmp_path / "empty_patch_stop_guard.json").read_text())
    assert state["generated_paths"] == [".yarn/install-state.gz"]


def test_stop_guard_rejects_python_bytecode_but_accepts_similar_source_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch = (
        "diff --git a/openlibrary/solr/__pycache__/query_utils.cpython-311.pyc "
        "b/openlibrary/solr/__pycache__/query_utils.cpython-311.pyc\n"
        "new file mode 100644\n"
        "Binary files /dev/null and "
        "b/openlibrary/solr/__pycache__/query_utils.cpython-311.pyc differ\n"
        "diff --git a/openlibrary/solr/__pycache__/metadata.json "
        "b/openlibrary/solr/__pycache__/metadata.json\n"
        "--- a/openlibrary/solr/__pycache__/metadata.json\n"
        "+++ b/openlibrary/solr/__pycache__/metadata.json\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/openlibrary/solr/standalone.pyc "
        "b/openlibrary/solr/standalone.pyc\n"
        "--- a/openlibrary/solr/standalone.pyc\n"
        "+++ b/openlibrary/solr/standalone.pyc\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/openlibrary/solr/query_utils.py "
        "b/openlibrary/solr/query_utils.py\n"
        "--- a/openlibrary/solr/query_utils.py\n"
        "+++ b/openlibrary/solr/query_utils.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/openlibrary/solr/cache.pyc.py "
        "b/openlibrary/solr/cache.pyc.py\n"
        "--- a/openlibrary/solr/cache.pyc.py\n"
        "+++ b/openlibrary/solr/cache.pyc.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    monkeypatch.setattr(guard, "_container_patch", lambda *args: patch)

    result = guard.evaluate_stop(_env(tmp_path, rejections=1))

    assert result == {"decision": "allow", "reason": "source_patch_present"}
    state = json.loads((tmp_path / "empty_patch_stop_guard.json").read_text())
    assert state["source_paths"] == [
        "openlibrary/solr/__pycache__/metadata.json",
        "openlibrary/solr/standalone.pyc",
        "openlibrary/solr/query_utils.py",
        "openlibrary/solr/cache.pyc.py",
    ]
    assert state["generated_paths"] == [
        "openlibrary/solr/__pycache__/query_utils.cpython-311.pyc",
    ]


def test_stop_guard_allows_hook_infrastructure_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args):
        raise RuntimeError("docker unavailable")

    monkeypatch.setattr(guard, "_container_patch", fail)

    result = guard.evaluate_stop(_env(tmp_path))

    assert result["decision"] == "allow"
    assert result["reason"] == "patch_guard_error"


def test_stop_guard_rejects_only_verified_missing_baseline_gitlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oid = "1" * 40
    patch = (
        "diff --git a/e b/e\n"
        "deleted file mode 160000\n"
        f"index {oid}..{'0' * 40}\n"
        "--- a/e\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        f"-Subproject commit {oid}\n"
    )
    monkeypatch.setattr(guard, "_container_patch", lambda *args: patch)
    monkeypatch.setattr(
        guard,
        "_container_gitlink_probe",
        lambda *args: {
            "status": "verified",
            "source_patch_sha256": "2" * 64,
            "paths": [
                {
                    "path": "e",
                    "old_oid": oid,
                    "base_oid": oid,
                    "probe_status": "verified",
                }
            ],
        },
    )

    result = guard.evaluate_stop(_env(tmp_path, rejections=1))

    assert result["decision"] == "deny"
    assert result["reason"] == "empty_source_patch"
    state = json.loads((tmp_path / "empty_patch_stop_guard.json").read_text())
    assert state.get("source_paths", []) == []
    assert state["generated_paths"] == ["e"]
    assert state["gitlink_probe"]["paths"][0]["probe_status"] == "verified"


def test_stop_guard_keeps_gitlink_when_baseline_oid_does_not_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oid = "1" * 40
    patch = (
        "diff --git a/e b/e\n"
        "deleted file mode 160000\n"
        f"index {oid}..{'0' * 40}\n"
        "--- a/e\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        f"-Subproject commit {oid}\n"
    )
    monkeypatch.setattr(guard, "_container_patch", lambda *args: patch)
    monkeypatch.setattr(
        guard,
        "_container_gitlink_probe",
        lambda *args: {
            "status": "baseline_mismatch",
            "source_patch_sha256": "2" * 64,
            "paths": [
                {
                    "path": "e",
                    "old_oid": oid,
                    "base_oid": "3" * 40,
                    "probe_status": "mismatch",
                }
            ],
        },
    )

    result = guard.evaluate_stop(_env(tmp_path, rejections=1))

    assert result == {"decision": "allow", "reason": "source_patch_present"}
    state = json.loads((tmp_path / "empty_patch_stop_guard.json").read_text())
    assert state["source_paths"] == ["e"]
    assert state["generated_paths"] == []
