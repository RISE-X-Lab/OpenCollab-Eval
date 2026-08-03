from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_code_test_support import TASK_IMAGE_ID, build_sidecar_fixture

from opencollab_eval.generation import recover_external_candidate as recovery

OLD_TASK = "solver-" + "1" * 32
NEW_TASK = "solver-" + "2" * 32


def _write_source(source: Path) -> dict:
    source.mkdir()
    (source / "claude.patch").write_text(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
        encoding="utf-8",
    )
    (source / "claude.prompt.md").write_text("solver prompt\n", encoding="utf-8")
    (source / "prompt.md").write_text("public task prompt\n", encoding="utf-8")
    (source / "solver_instance.json").write_text(
        json.dumps({"instance_id": OLD_TASK, "problem_statement": "fix it"}) + "\n",
        encoding="utf-8",
    )
    sidecar = build_sidecar_fixture(source)
    sidecar["invocation_binding"]["raw_patch_sha256"] = hashlib.sha256(
        (source / "claude.patch").read_bytes()
    ).hexdigest()
    (source / "external_solver.sidecar.json").write_text(
        json.dumps(sidecar) + "\n", encoding="utf-8"
    )
    (source / "external_solver.required.json").write_text(
        json.dumps(
            {
                "solver": "claude-code",
                "solver_task_id": OLD_TASK,
                "expected_model": "glm-5.2",
                "expected_runtime_image_id": "sha256:" + "a" * 64,
                "task_image_id": TASK_IMAGE_ID,
                "network_name": "oc-claude-net-" + OLD_TASK,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return sidecar


def _current_inputs(tmp_path: Path) -> tuple[Path, Path]:
    instance = tmp_path / "current-instance.json"
    prompt = tmp_path / "current-prompt.md"
    instance.write_text(
        json.dumps({"instance_id": NEW_TASK, "problem_statement": "fix it"}) + "\n",
        encoding="utf-8",
    )
    prompt.write_text("public task prompt\n", encoding="utf-8")
    return instance, prompt


def _expectations(source: Path) -> dict[str, str]:
    sidecar_path = source / "external_solver.sidecar.json"
    sidecar = json.loads(sidecar_path.read_text())
    binding = sidecar["invocation_binding"]
    return {
        "expected_source_solver_task_id": binding["solver_task_id"],
        "expected_raw_patch_sha256": binding["raw_patch_sha256"],
        "expected_candidate_tree": binding["candidate_tree"],
        "expected_source_sidecar_sha256": hashlib.sha256(
            sidecar_path.read_bytes()
        ).hexdigest(),
    }


def test_recovery_revalidates_and_rebinds_without_mutating_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    sidecar = _write_source(source)
    instance, prompt = _current_inputs(tmp_path)
    source_sidecar_sha = hashlib.sha256(
        (source / "external_solver.sidecar.json").read_bytes()
    ).hexdigest()
    docker_calls: list[tuple[str, ...]] = []

    def fake_docker(*arguments: str):
        docker_calls.append(arguments)
        stdout = ""
        if arguments[-2:] == ("rev-parse", "HEAD"):
            stdout = sidecar["invocation_binding"]["anonymous_head"] + "\n"
        elif arguments[-2:] == ("rev-parse", "HEAD^{tree}"):
            stdout = sidecar["invocation_binding"]["base_tree"] + "\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(recovery, "_run_docker", fake_docker)
    monkeypatch.setattr(
        recovery.container_quiescence,
        "container_image_id",
        lambda _container_id: TASK_IMAGE_ID,
    )
    output = tmp_path / "output"

    manifest = recovery.recover_candidate(
        source_dir=source,
        output_dir=output,
        container_id="a" * 64,
        solver_task_id=NEW_TASK,
        instance_file=instance,
        prompt_file=prompt,
        **_expectations(source),
    )

    assert hashlib.sha256(
        (source / "external_solver.sidecar.json").read_bytes()
    ).hexdigest() == source_sidecar_sha
    rebound = json.loads((output / "external_solver.sidecar.json").read_text())
    required = json.loads((output / "external_solver.required.json").read_text())
    assert rebound["source_invocation_binding"]["solver_task_id"] == OLD_TASK
    assert rebound["invocation_binding"]["solver_task_id"] == NEW_TASK
    assert rebound["candidate_ready"] is False
    assert required["solver_task_id"] == NEW_TASK
    assert "network_name" not in required
    assert manifest["source_sidecar_sha256"] == source_sidecar_sha
    assert any("apply" in arguments for arguments in docker_calls)


@pytest.mark.parametrize("drift", ["prompt", "instance", "image"])
def test_recovery_rejects_task_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    source = tmp_path / "source"
    _write_source(source)
    instance, prompt = _current_inputs(tmp_path)
    if drift == "prompt":
        prompt.write_text("different task\n", encoding="utf-8")
    elif drift == "instance":
        instance.write_text(
            json.dumps({"instance_id": NEW_TASK, "problem_statement": "different"}),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        recovery.container_quiescence,
        "container_image_id",
        lambda _container_id: "sha256:" + ("e" if drift == "image" else "d") * 64,
    )

    with pytest.raises(ValueError, match="does not match"):
        recovery.recover_candidate(
            source_dir=source,
            output_dir=tmp_path / "output",
            container_id="a" * 64,
            solver_task_id=NEW_TASK,
            instance_file=instance,
            prompt_file=prompt,
            **_expectations(source),
        )


def test_recovery_rejects_required_task_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _write_source(source)
    required_path = source / "external_solver.required.json"
    required = json.loads(required_path.read_text())
    required["solver_task_id"] = "solver-" + "3" * 32
    required_path.write_text(json.dumps(required) + "\n", encoding="utf-8")
    instance, prompt = _current_inputs(tmp_path)
    monkeypatch.setattr(
        recovery.container_quiescence,
        "container_image_id",
        lambda _container_id: TASK_IMAGE_ID,
    )

    with pytest.raises(ValueError, match="controller task binding mismatch"):
        recovery.recover_candidate(
            source_dir=source,
            output_dir=tmp_path / "output",
            container_id="a" * 64,
            solver_task_id=NEW_TASK,
            instance_file=instance,
            prompt_file=prompt,
            **_expectations(source),
        )


@pytest.mark.parametrize(
    "field",
    [
        "expected_source_solver_task_id",
        "expected_raw_patch_sha256",
        "expected_candidate_tree",
        "expected_source_sidecar_sha256",
    ],
)
def test_recovery_requires_operator_bound_source_identity(
    tmp_path: Path, field: str
) -> None:
    source = tmp_path / "source"
    _write_source(source)
    instance, prompt = _current_inputs(tmp_path)
    expected = _expectations(source)
    expected[field] = (
        "solver-" + "f" * 32
        if field == "expected_source_solver_task_id"
        else "f" * (40 if field == "expected_candidate_tree" else 64)
    )

    with pytest.raises(ValueError, match="does not match expectation"):
        recovery.recover_candidate(
            source_dir=source,
            output_dir=tmp_path / "output",
            container_id="a" * 64,
            solver_task_id=NEW_TASK,
            instance_file=instance,
            prompt_file=prompt,
            **expected,
        )
