from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from opencollab_eval.generation import external_solver_containers as esc

TASK_ID = "solver-" + "1" * 32
TEST_CID = "a" * 64
RUNTIME_CID = "b" * 64
RELAY_CID = "c" * 64
GATEWAY_CID = "d" * 64
PROBE_CID = "e" * 64
OWNERS = {
    TEST_CID: "claude-code-external",
    RUNTIME_CID: "claude-code-runtime",
    RELAY_CID: "claude-code-relay",
    GATEWAY_CID: "claude-code-gateway",
}


def _prepare(output_dir: Path, *, cidfiles: bool = True) -> None:
    output_dir.mkdir(exist_ok=True)
    (output_dir / "external_solver.required.json").write_text(
        json.dumps({"solver_task_id": TASK_ID}),
        encoding="utf-8",
    )
    if cidfiles:
        (output_dir / "test-container.id").write_text(TEST_CID + "\n", encoding="utf-8")
        (output_dir / "runtime-container.id").write_text(RUNTIME_CID + "\n", encoding="utf-8")
        (output_dir / "relay-container.id").write_text(RELAY_CID + "\n", encoding="utf-8")
        (output_dir / "gateway-container.id").write_text(GATEWAY_CID + "\n", encoding="utf-8")


def _completed(command: list[str], returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def _docker_state(
    containers: dict[str, str],
    *,
    inspect_error: str = "",
    post_remove_error: str = "",
    auto_remove_race: bool = False,
):
    state = dict(containers)
    removed: list[str] = []

    def docker(command: list[str], **_kwargs: object):
        if command[:3] == ["docker", "ps", "-aq"]:
            return _completed(command, 0, "".join(f"{cid}\n" for cid in state))
        cid = command[-1]
        if command[:3] == ["docker", "rm", "-f"]:
            state.pop(cid, None)
            removed.append(cid)
            if auto_remove_race:
                return _completed(command, 1, stderr="Error: No such container")
            return _completed(command, 0, cid + "\n")
        if inspect_error and cid in state:
            return _completed(command, 1, stderr=inspect_error)
        if cid not in state:
            if post_remove_error and cid in removed:
                return _completed(command, 1, stderr=post_remove_error)
            return _completed(command, 1, stderr="Error: No such object")
        labels = {
            "opencollab.owner": state[cid],
            "opencollab.solver_task_id": TASK_ID,
        }
        return _completed(command, 0, json.dumps(labels))

    return docker, state, removed


def test_cleanup_without_external_solver_marker_needs_no_docker_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: object, **_kwargs: object):
        raise AssertionError("docker must not be called")

    monkeypatch.setattr(esc.subprocess, "run", unexpected)
    assert esc.cleanup_external_solver_containers(tmp_path) == {
        "proven": True,
        "containers": [],
        "label_query": "not_required",
    }


def test_cleanup_records_absent_full_cids_then_removes_restart_blockers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(tmp_path)
    docker, _state, _removed = _docker_state({})
    monkeypatch.setattr(esc.subprocess, "run", docker)

    result = esc.cleanup_external_solver_containers(tmp_path)

    cid_records = [record for record in result["containers"] if record.get("cid")]
    assert result["proven"] is True
    assert {record["cid"] for record in cid_records} == set(OWNERS)
    assert all(record["status"] == "already_absent" for record in cid_records)
    assert all(record["cidfile_removed"] is True for record in cid_records)
    assert not (tmp_path / "test-container.id").exists()
    assert not (tmp_path / "runtime-container.id").exists()
    assert not (tmp_path / "relay-container.id").exists()
    assert not (tmp_path / "gateway-container.id").exists()


@pytest.mark.parametrize("error", ["Cannot connect to the Docker daemon", "permission denied"])
def test_cleanup_keeps_cidfiles_when_inspection_cannot_prove_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: str,
) -> None:
    _prepare(tmp_path)
    docker, _state, _removed = _docker_state(OWNERS, inspect_error=error)
    monkeypatch.setattr(esc.subprocess, "run", docker)
    result = esc.cleanup_external_solver_containers(tmp_path)

    assert result["proven"] is False
    assert any(record["status"] == "inspect_failed" for record in result["containers"])
    assert (tmp_path / "test-container.id").read_text().strip() == TEST_CID
    assert (tmp_path / "runtime-container.id").read_text().strip() == RUNTIME_CID


def test_cleanup_refuses_container_with_mismatched_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(tmp_path)
    docker, state, removed = _docker_state({TEST_CID: "another-run"})
    monkeypatch.setattr(esc.subprocess, "run", docker)
    result = esc.cleanup_external_solver_containers(tmp_path)

    assert result["proven"] is False
    assert any(record["status"] == "owner_mismatch" for record in result["containers"])
    assert TEST_CID in state
    assert removed == []


def test_cleanup_removes_owned_containers_and_verifies_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(tmp_path)
    docker, state, removed = _docker_state(OWNERS)
    monkeypatch.setattr(esc.subprocess, "run", docker)
    result = esc.cleanup_external_solver_containers(tmp_path)

    cid_records = [record for record in result["containers"] if record.get("cid")]
    assert result["proven"] is True
    assert state == {}
    assert set(removed) == set(OWNERS)
    assert all(record["status"] == "removed" for record in cid_records)
    assert all(record["absent_verified"] is True for record in cid_records)
    assert all(record["cidfile_removed"] is True for record in cid_records)


def test_label_query_recovers_container_created_before_cidfile_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(tmp_path, cidfiles=False)
    docker, state, removed = _docker_state(OWNERS)
    monkeypatch.setattr(esc.subprocess, "run", docker)

    result = esc.cleanup_external_solver_containers(tmp_path)

    assert result["proven"] is True
    assert state == {}
    assert set(removed) == set(OWNERS)
    assert {record["source"] for record in result["containers"] if record.get("cid")} == {
        "label_query"
    }


def test_label_query_recovers_from_partially_written_cidfiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(tmp_path)
    for filename in ("test-container.id", "runtime-container.id"):
        (tmp_path / filename).write_text("partial", encoding="utf-8")
    docker, state, removed = _docker_state(OWNERS)
    monkeypatch.setattr(esc.subprocess, "run", docker)

    result = esc.cleanup_external_solver_containers(tmp_path)

    assert result["proven"] is True
    assert state == {}
    assert set(removed) == set(OWNERS)
    assert all(not (tmp_path / filename).exists() for filename in esc.EXPECTED_OWNERS)
    invalid = [record for record in result["containers"] if record.get("status") == "invalid_cid"]
    assert {record["cidfile"] for record in invalid} == {
        "test-container.id",
        "runtime-container.id",
    }


def test_label_query_cleans_an_interrupted_short_lived_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(tmp_path, cidfiles=False)
    docker, state, removed = _docker_state({PROBE_CID: "claude-code-probe"})
    monkeypatch.setattr(esc.subprocess, "run", docker)

    result = esc.cleanup_external_solver_containers(tmp_path)

    assert result["proven"] is True
    assert state == {}
    assert removed == [PROBE_CID]


def test_cleanup_fails_closed_when_post_remove_inspection_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(tmp_path, cidfiles=False)
    (tmp_path / "test-container.id").write_text(TEST_CID, encoding="utf-8")
    docker, _state, _removed = _docker_state(
        {TEST_CID: "claude-code-external"},
        post_remove_error="Cannot connect to the Docker daemon",
    )
    monkeypatch.setattr(esc.subprocess, "run", docker)
    result = esc.cleanup_external_solver_containers(tmp_path)

    record = next(record for record in result["containers"] if record.get("cid") == TEST_CID)
    assert result["proven"] is False
    assert record["status"] == "cleanup_failed"
    assert record["absent_verified"] is False
    assert (tmp_path / "test-container.id").exists()


def test_cleanup_accepts_runtime_auto_remove_race_after_absence_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(tmp_path, cidfiles=False)
    (tmp_path / "runtime-container.id").write_text(RUNTIME_CID, encoding="utf-8")
    docker, state, _removed = _docker_state(
        {RUNTIME_CID: "claude-code-runtime"},
        auto_remove_race=True,
    )
    monkeypatch.setattr(esc.subprocess, "run", docker)

    result = esc.cleanup_external_solver_containers(tmp_path)

    record = next(record for record in result["containers"] if record.get("cid") == RUNTIME_CID)
    assert result["proven"] is True
    assert state == {}
    assert record["status"] == "removed"
    assert record["remove_returncode"] == 1
    assert record["absent_verified"] is True


def test_cleanup_accepts_network_auto_remove_race_after_absence_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(tmp_path, cidfiles=False)
    marker = tmp_path / "external_solver.required.json"
    marker.write_text(
        json.dumps(
            {
                "solver_task_id": TASK_ID,
                "network_name": f"oc-claude-net-{TASK_ID}",
            }
        ),
        encoding="utf-8",
    )
    network_inspections = 0

    def docker(command: list[str], **_kwargs: object):
        nonlocal network_inspections
        if command[:3] == ["docker", "ps", "-aq"]:
            return _completed(command, 0)
        if command[:3] == ["docker", "network", "rm"]:
            return _completed(command, 1, stderr="Error: No such network")
        if command[:3] == ["docker", "network", "inspect"]:
            network_inspections += 1
            if network_inspections == 1:
                labels = {
                    "opencollab.owner": "claude-code-network",
                    "opencollab.solver_task_id": TASK_ID,
                }
                return _completed(command, 0, json.dumps(labels))
            return _completed(command, 1, stderr="Error: No such network")
        raise AssertionError(command)

    monkeypatch.setattr(esc.subprocess, "run", docker)
    result = esc.cleanup_external_solver_containers(tmp_path)

    assert result["proven"] is True
    assert result["network"]["status"] == "removed"
    assert result["network"]["absent_verified"] is True


def test_cleanup_accepts_docker_network_not_found_as_verified_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(tmp_path, cidfiles=False)
    network_name = f"oc-claude-net-{TASK_ID}"
    (tmp_path / "external_solver.required.json").write_text(
        json.dumps({"solver_task_id": TASK_ID, "network_name": network_name}),
        encoding="utf-8",
    )

    def docker(command: list[str], **_kwargs: object):
        if command[:3] == ["docker", "ps", "-aq"]:
            return _completed(command, 0)
        if command[:3] == ["docker", "network", "inspect"]:
            return _completed(
                command,
                1,
                stderr=f"Error response from daemon: network {network_name} not found",
            )
        raise AssertionError(command)

    monkeypatch.setattr(esc.subprocess, "run", docker)

    result = esc.cleanup_external_solver_containers(tmp_path)

    assert result["proven"] is True
    assert result["network"] == {
        "proven": True,
        "status": "already_absent",
        "name": network_name,
    }


def test_cleanup_rejects_network_not_found_for_another_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(tmp_path, cidfiles=False)
    network_name = f"oc-claude-net-{TASK_ID}"
    (tmp_path / "external_solver.required.json").write_text(
        json.dumps({"solver_task_id": TASK_ID, "network_name": network_name}),
        encoding="utf-8",
    )

    def docker(command: list[str], **_kwargs: object):
        if command[:3] == ["docker", "ps", "-aq"]:
            return _completed(command, 0)
        if command[:3] == ["docker", "network", "inspect"]:
            return _completed(command, 1, stderr="network oc-claude-net-unrelated not found")
        raise AssertionError(command)

    monkeypatch.setattr(esc.subprocess, "run", docker)

    result = esc.cleanup_external_solver_containers(tmp_path)

    assert result["proven"] is False
    assert result["network"]["status"] == "inspect_failed"


def test_cleanup_reports_cidfile_deletion_failure_after_absence_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(tmp_path)
    docker, _state, _removed = _docker_state({})
    monkeypatch.setattr(esc.subprocess, "run", docker)
    original_unlink = Path.unlink

    def fail_test_cidfile(path: Path, *args: object, **kwargs: object) -> None:
        if path.name == "test-container.id":
            raise OSError("read-only output directory")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_test_cidfile)
    result = esc.cleanup_external_solver_containers(tmp_path)

    assert result["proven"] is False
    test_record = next(record for record in result["containers"] if record.get("cid") == TEST_CID)
    assert test_record["cidfile_removed"] is False
    assert "read-only output directory" in test_record["cidfile_remove_error"]
    assert (tmp_path / "test-container.id").exists()
