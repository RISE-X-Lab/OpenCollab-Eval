from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from package_test_support import module_path

_SWEBENCH_DIR = module_path("opencollab_eval.generation.gen_prediction").parent
if str(_SWEBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_SWEBENCH_DIR))

from opencollab_eval.generation import openhands_require_patch as guard  # noqa: E402


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
