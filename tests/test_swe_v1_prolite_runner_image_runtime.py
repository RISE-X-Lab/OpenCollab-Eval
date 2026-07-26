from __future__ import annotations

import inspect
import sys

from swe_v1_prolite_runner_test_support import (
    _remote_namespace,
    remote_eval_script,
)

from opencollab_eval.engine import swe_v1_remote_evaluation


def test_runtime_dependency_probe_has_a_hard_output_bound(tmp_path):
    namespace = _remote_namespace(tmp_path)

    result = namespace["_run_bounded"](
        [sys.executable, "-c", "print('x' * 9000)"],
        timeout=10,
        limit=8192,
    )

    assert result["returncode"] == 125
    assert len(result["stdout"].encode("utf-8")) == 8192
    assert result["stderr"] == "runtime dependency probe output exceeded 8192 bytes"


def test_ensure_image_pulls_missing_image(tmp_path):
    namespace = _remote_namespace(tmp_path)
    existing: set[str] = set()
    calls: list[list[str]] = []

    def fake_image_exists(image):
        return image in existing

    def fake_run(command, timeout=60):
        calls.append(command)
        if command[:2] == ["docker", "pull"]:
            existing.add(command[2])
            return {"returncode": 0, "stdout": "", "stderr": ""}
        return {"returncode": 1, "stdout": "", "stderr": "unexpected"}

    namespace["image_exists"] = fake_image_exists
    namespace["immutable_image_id"] = lambda image: "sha256:" + "8" * 64
    namespace["run"] = fake_run

    result = namespace["ensure_image"]("example/image:tag")

    assert result["ok"] is True
    assert result["pulled"] is True
    assert calls == [["docker", "pull", "example/image:tag"]]


def test_immutable_image_id_requires_a_full_docker_digest(tmp_path):
    namespace = _remote_namespace(tmp_path)
    namespace["run"] = lambda command, timeout=60: {
        "returncode": 0,
        "stdout": "sha256:" + "7" * 64 + "\n",
        "stderr": "",
    }

    assert namespace["immutable_image_id"]("example/image:tag") == "sha256:" + "7" * 64

    namespace["run"] = lambda command, timeout=60: {
        "returncode": 0,
        "stdout": "example/image:latest\n",
        "stderr": "",
    }
    assert namespace["immutable_image_id"]("example/image:tag") == ""


def test_ensure_image_rejects_a_mutable_or_unresolved_local_identity(tmp_path):
    namespace = _remote_namespace(tmp_path)
    namespace["image_exists"] = lambda _image: True
    namespace["immutable_image_id"] = lambda _image: ""

    result = namespace["ensure_image"]("example/image:tag")

    assert result == {
        "ok": False,
        "image": "example/image:tag",
        "pulled": False,
        "reason": "invalid_image_identity",
    }


def test_image_for_row_uses_configured_repository_for_bare_tags(tmp_path):
    namespace = _remote_namespace(
        tmp_path,
        image_repository="registry.example/swebench",
    )

    assert namespace["image_for_row"]({"dockerhub_tag": "task-tag"}) == (
        "registry.example/swebench:task-tag"
    )
    assert namespace["image_for_row"]({"instance_id": "instance_task-id"}) == (
        "registry.example/swebench:task-id"
    )
    assert namespace["image_for_row"](
        {"dockerhub_tag": "public.example/team/image:tag"}
    ) == ("public.example/team/image:tag")


def test_image_exists_uses_bounded_docker_inspect(tmp_path):
    namespace = _remote_namespace(tmp_path)
    calls = []

    def fake_run(command, timeout=60):
        calls.append((command, timeout))
        return {"returncode": 124, "stdout": "", "stderr": "timed out"}

    namespace["run"] = fake_run

    assert namespace["image_exists"]("example/image:tag") is False
    assert calls == [(["docker", "image", "inspect", "example/image:tag"], 120)]


def test_image_workdir_preflight_is_offline_owned_and_cleaned_after_timeout(tmp_path):
    namespace = _remote_namespace(tmp_path)
    container_id = "a" * 64
    calls = []
    removed = False

    def fake_run(command, timeout=60):
        nonlocal removed
        calls.append((command, timeout))
        if command[:3] == ["timeout", "120", "docker"]:
            return {"returncode": 124, "stdout": "", "stderr": "timed out"}
        if command[:2] == ["docker", "inspect"]:
            reference = command[-1]
            if removed or reference == container_id:
                return {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "No such container",
                }
            return {
                "returncode": 0,
                "stdout": (
                    f"{container_id}\t{namespace['owner_nonce']}\t"
                    f"{namespace['PREFLIGHT_SCHEMA']}"
                ),
                "stderr": "",
            }
        if command[:4] == ["docker", "rm", "-f", "--"]:
            removed = True
            return {"returncode": 0, "stdout": container_id, "stderr": ""}
        raise AssertionError(command)

    namespace["run"] = fake_run

    result = namespace["image_repo_workdir_status"]("registry.example/image:tag")

    docker_run = calls[0][0]
    assert result["ok"] is False
    assert result["container_cleanup"]["ok"] is True
    assert docker_run[docker_run.index("--network") + 1] == "none"
    assert "--cidfile" in docker_run
    assert "--name" in docker_run
    expected_owner = f"{namespace['PREFLIGHT_OWNER_LABEL']}={namespace['owner_nonce']}"
    assert expected_owner in docker_run
    assert ["docker", "rm", "-f", "--", container_id] in [
        call for call, _timeout in calls
    ]


def test_runtime_dependency_identity_probe_binds_an_ignored_file_to_the_image(tmp_path):
    namespace = _remote_namespace(tmp_path)
    calls = []

    def fake_probe(command, timeout=60):
        calls.append((command, timeout))
        return {
            "returncode": 0,
            "stdout": (
                '[{"root":"package.json","content_sha256":"'
                + "a" * 64
                + '"},{"root":"config.json","content_sha256":"'
                + "b" * 64
                + '"}]'
            ),
            "stderr": "",
        }

    namespace["_run_bounded"] = fake_probe
    namespace["run"] = lambda _command, timeout=60: {
        "returncode": 1,
        "stdout": "",
        "stderr": "No such container",
    }
    image_id = "sha256:" + "1" * 64
    result = namespace["image_runtime_dependency_identities"](
        image_id,
        [
            {
                "root": "package.json",
                "required_paths": ["package.json"],
                "kind": "file",
                "candidate_protected": False,
            },
            {
                "root": "config.json",
                "required_paths": ["config.json"],
                "kind": "file",
                "candidate_protected": False,
            },
        ],
    )

    assert result["ok"] is True
    assert result["document"] == {
        "schema": "opencollab.runtime_dependency_identities.v1",
        "image_id": image_id,
        "entries": [
            {"root": "package.json", "content_sha256": "a" * 64},
            {"root": "config.json", "content_sha256": "b" * 64},
        ],
    }
    docker_run = calls[0][0]
    assert docker_run[docker_run.index("--network") + 1] == "none"
    assert docker_run[docker_run.index("--entrypoint") + 1] == "python3"
    probe_source = docker_run[docker_run.index("-c") + 1]
    assert '"GIT_CONFIG_GLOBAL": "/dev/null"' in probe_source
    assert '"GIT_CONFIG_NOSYSTEM": "1"' in probe_source
    assert '"GIT_CONFIG_KEY_0": "safe.directory"' in probe_source


def test_runtime_dependency_identity_probe_rejects_malformed_image_evidence(tmp_path):
    namespace = _remote_namespace(tmp_path)

    def fake_probe(_command, timeout=60):
        return {
            "returncode": 0,
            "stdout": '[{"root":"package.json","content_sha256":"short"}]',
            "stderr": "",
        }

    namespace["_run_bounded"] = fake_probe
    namespace["run"] = lambda _command, timeout=60: {
        "returncode": 1,
        "stdout": "",
        "stderr": "No such container",
    }

    result = namespace["image_runtime_dependency_identities"](
        "sha256:" + "1" * 64,
        [
            {
                "root": "package.json",
                "required_paths": ["package.json"],
                "kind": "file",
                "candidate_protected": False,
            }
        ],
    )

    assert result["ok"] is False


def test_preflight_cleanup_refuses_container_without_matching_owner_label(tmp_path):
    namespace = _remote_namespace(tmp_path)
    container_id = "b" * 64
    calls = []

    def fake_run(command, timeout=60):
        calls.append(command)
        if command[:2] == ["docker", "inspect"]:
            return {
                "returncode": 0,
                "stdout": (
                    f"{container_id}\tforeign-owner\t"
                    f"{namespace['PREFLIGHT_SCHEMA']}"
                ),
                "stderr": "",
            }
        raise AssertionError(command)

    namespace["run"] = fake_run
    cidfile = namespace["base_run_dir"] / "foreign.cid"

    result = namespace["cleanup_preflight_container"](
        cidfile,
        "foreign-container",
    )

    assert result["ok"] is False
    assert result["status"] == "ownership_unproven"
    assert all(command[:2] != ["docker", "rm"] for command in calls)


def test_remote_runner_bootstraps_redis_for_nodebb(tmp_path):
    namespace = _remote_namespace(tmp_path)

    script = namespace["prolite_service_bootstrap"]({"repo": "NodeBB/NodeBB"})

    assert "redis-server" in script
    assert "127.0.0.1:6379" in script
    assert namespace["prolite_service_bootstrap"]({"repo": "python/cpython"}) == ""


def test_prolite_eval_commands_use_separate_input_files_not_fixed_heredocs():
    source = inspect.getsource(swe_v1_remote_evaluation.eval_for_task_once)
    shell = remote_eval_script.DIRECT_EVAL_SCRIPT

    assert "<<'SERVICE'" not in source
    assert "<<'BEFORE'" not in source
    assert 'input_dir / "service_bootstrap.sh"' in source
    assert 'input_dir / "before_repo.sh"' in source
    assert '"runtime_dependency_specs.json"' in source
    assert '"runtime_dependencies": artifacts["runtime_dependencies"]' in source
    assert '"runtime_dependency_identities": runtime_dependency_identities' in source
    assert "bash /eval_input/service_bootstrap.sh" in shell
    assert "eval_runtime_dependencies.py stash" in shell
    assert "eval_runtime_dependencies.py restore" in shell
    first_stash = shell.index("eval_runtime_dependencies.py stash")
    preparation_restore = shell.index("eval_runtime_dependencies.py restore")
    before_repo = shell.index("public_preparation_runner.py")
    second_stash = shell.index("eval_runtime_dependencies.py stash", first_stash + 1)
    assert first_stash < preparation_restore < before_repo < second_stash
