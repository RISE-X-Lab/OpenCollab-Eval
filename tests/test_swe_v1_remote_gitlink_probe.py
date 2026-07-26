from __future__ import annotations

import hashlib
import json
from pathlib import Path

from opencollab_eval.engine import swe_v1_remote_gitlink_probe as probe
from opencollab_eval.patch_gitlinks import (
    filter_verified_gitlink_deletions,
    gitlink_deletion_candidates,
)


def _gitlink_delete(path: str, oid: str, *, mode: str = "160000") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"deleted file mode {mode}\n"
        f"index {oid}..{'0' * len(oid)}\n"
        f"--- a/{path}\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        f"-Subproject commit {oid}\n"
    )


def _source_change(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )


def _configure_probe(monkeypatch, tmp_path: Path, output: str, *, returncode: int = 0):
    calls = []

    def fake_run(command, timeout=60):
        calls.append((command, timeout))
        return {"returncode": returncode, "stdout": output, "stderr": ""}

    monkeypatch.setattr(probe, "base_run_dir", tmp_path)
    monkeypatch.setattr(probe, "owner_nonce", "a" * 32)
    monkeypatch.setattr(probe, "run", fake_run)
    monkeypatch.setattr(
        probe,
        "cleanup_preflight_container",
        lambda *args: {"ok": True, "status": "absent"},
    )
    return calls


def test_gitlink_filter_requires_exact_deletion_shape() -> None:
    oid = "1" * 40
    valid = _gitlink_delete("vendor/infogami", oid)
    wrong_mode = _gitlink_delete("vendor/not-a-gitlink", oid, mode="100644")
    wrong_commit = valid.replace(f"Subproject commit {oid}", f"Subproject commit {'2' * 40}")

    assert gitlink_deletion_candidates(valid) == [
        {"block_index": 0, "path": "vendor/infogami", "old_oid": oid}
    ]
    assert gitlink_deletion_candidates(wrong_mode) == []
    assert gitlink_deletion_candidates(wrong_commit) == []


def test_gitlink_candidate_preserves_a_literal_b_directory_segment() -> None:
    oid = "1" * 40
    path = "vendor/x b/component"

    assert gitlink_deletion_candidates(_gitlink_delete(path, oid)) == [
        {"block_index": 0, "path": path, "old_oid": oid}
    ]


def test_gitlink_probe_filters_only_matching_baseline_oid(monkeypatch, tmp_path: Path) -> None:
    oid = "1" * 40
    source = _gitlink_delete("vendor/infogami", oid) + _source_change(
        "openlibrary/solr/query_utils.py"
    )
    candidates = gitlink_deletion_candidates(source)
    output = f"160000 commit {oid}\tvendor/infogami\0"
    calls = _configure_probe(monkeypatch, tmp_path, output)

    result = probe.probe_gitlink_deletions(
        task="instance_internetarchive__openlibrary-1",
        image="registry.example/openlibrary:latest",
        image_id="sha256:" + "9" * 64,
        base_commit="2" * 40,
        source_patch_sha256="3" * 64,
        candidates=candidates,
    )
    filtered, evidence = filter_verified_gitlink_deletions(source, result["verified"])

    assert result["ok"] is True
    assert result["probe"]["status"] == "verified"
    assert result["probe"]["image_id"] == "sha256:" + "9" * 64
    assert len(result["probe"]["probe_output_sha256"]) == 64
    assert len(result["probe"]["probe_script_sha256"]) == 64
    assert len(result["probe"]["probe_command_sha256"]) == 64
    assert result["probe"]["probe_command_sha256"] == hashlib.sha256(
        json.dumps(
            result["probe"]["probe_argv"],
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert result["probe"]["probe_output_sha256"] == hashlib.sha256(
        json.dumps(
            result["probe"]["probe_parsed_output"],
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    canonical_command = result["probe"]["probe_argv"]
    assert "--name" not in canonical_command
    assert "--cidfile" not in canonical_command
    assert "--label" not in canonical_command
    assert "timeout" not in canonical_command
    assert "a" * 32 not in " ".join(canonical_command)
    assert result["probe"]["paths"] == [
        {
            "block_index": 0,
            "path": "vendor/infogami",
            "old_oid": oid,
            "base_mode": "160000",
            "base_type": "commit",
            "base_oid": oid,
            "probe_status": "verified",
        }
    ]
    assert "vendor/infogami" not in filtered
    assert "openlibrary/solr/query_utils.py" in filtered
    assert evidence == [
        {
            "path": "vendor/infogami",
            "reason": "missing_snapshot_gitlink",
            "old_oid": oid,
            "base_oid": oid,
            "probe_status": "verified",
        }
    ]
    command = calls[0][0]
    assert "--read-only" in command
    assert command[command.index("--network") + 1] == "none"
    assert command[-3:] == ["2" * 40, "--", "vendor/infogami"]


def test_gitlink_probe_unsets_git_redirection_environment() -> None:
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    ):
        assert name in probe._PROBE_SCRIPT


def test_gitlink_probe_rejects_forged_oid(monkeypatch, tmp_path: Path) -> None:
    patch_oid = "1" * 40
    base_oid = "2" * 40
    candidates = gitlink_deletion_candidates(_gitlink_delete("vendor/infogami", patch_oid))
    _configure_probe(
        monkeypatch,
        tmp_path,
        f"160000 commit {base_oid}\tvendor/infogami\0",
    )

    result = probe.probe_gitlink_deletions(
        task="instance_repo-1",
        image="registry.example/repo:latest",
        image_id="sha256:" + "9" * 64,
        base_commit="3" * 40,
        source_patch_sha256="4" * 64,
        candidates=candidates,
    )

    assert result["ok"] is False
    assert result["status"] == "gitlink_probe_baseline_mismatch"
    assert result["probe"]["paths"][0]["old_oid"] == patch_oid
    assert result["probe"]["paths"][0]["base_oid"] == base_oid
    assert result["probe"]["paths"][0]["probe_status"] == "mismatch"


def test_gitlink_probe_rejects_non_gitlink_tree_entry(monkeypatch, tmp_path: Path) -> None:
    oid = "1" * 40
    candidates = gitlink_deletion_candidates(_gitlink_delete("vendor/infogami", oid))
    _configure_probe(
        monkeypatch,
        tmp_path,
        f"100644 blob {oid}\tvendor/infogami\0",
    )

    result = probe.probe_gitlink_deletions(
        task="instance_repo-1",
        image="registry.example/repo:latest",
        image_id="sha256:" + "9" * 64,
        base_commit="2" * 40,
        source_patch_sha256="3" * 64,
        candidates=candidates,
    )

    assert result["ok"] is False
    assert result["probe"]["paths"][0]["base_mode"] == "100644"
    assert result["probe"]["paths"][0]["probe_status"] == "mismatch"


def test_gitlink_probe_failure_is_fail_closed(monkeypatch, tmp_path: Path) -> None:
    oid = "1" * 40
    candidates = gitlink_deletion_candidates(_gitlink_delete("vendor/infogami", oid))
    _configure_probe(monkeypatch, tmp_path, "", returncode=125)

    result = probe.probe_gitlink_deletions(
        task="instance_repo-1",
        image="registry.example/repo:latest",
        image_id="sha256:" + "9" * 64,
        base_commit="2" * 40,
        source_patch_sha256="3" * 64,
        candidates=candidates,
    )

    assert result["ok"] is False
    assert result["status"] == "gitlink_probe_execution_failed"
    assert result["probe"]["status"] == "probe_execution_failed"


def test_task41_shape_keeps_agent_note_and_real_source(monkeypatch, tmp_path: Path) -> None:
    first_oid = "1" * 40
    second_oid = "2" * 40
    source = (
        _source_change("AGENTS.md")
        + _gitlink_delete("vendor/infogami", first_oid)
        + _source_change("openlibrary/solr/query_utils.py")
        + _gitlink_delete("vendor/js/wmd", second_oid)
    )
    candidates = gitlink_deletion_candidates(source)
    output = (
        f"160000 commit {first_oid}\tvendor/infogami\0"
        f"160000 commit {second_oid}\tvendor/js/wmd\0"
    )
    _configure_probe(monkeypatch, tmp_path, output)

    result = probe.probe_gitlink_deletions(
        task="instance_internetarchive__openlibrary-1",
        image="registry.example/openlibrary:latest",
        image_id="sha256:" + "9" * 64,
        base_commit="3" * 40,
        source_patch_sha256="4" * 64,
        candidates=candidates,
    )
    filtered, evidence = filter_verified_gitlink_deletions(source, result["verified"])

    assert result["ok"] is True
    assert [item["path"] for item in evidence] == ["vendor/infogami", "vendor/js/wmd"]
    assert "AGENTS.md" in filtered
    assert "openlibrary/solr/query_utils.py" in filtered
    assert "vendor/" not in filtered


def test_eval_patch_selection_records_source_child_and_probe_binding(monkeypatch) -> None:
    oid = "1" * 40
    source = _gitlink_delete("vendor/infogami", oid) + _source_change(
        "openlibrary/solr/query_utils.py"
    )
    monkeypatch.setattr(probe, "current_generation_proof_valid", lambda *args: False)
    monkeypatch.setattr(probe, "image_for_row", lambda row: "registry.example/task:latest")
    monkeypatch.setattr(probe, "ensure_image", lambda image: {"ok": True, "image": image})
    monkeypatch.setattr(
        probe,
        "resolve_local_image_id",
        lambda image: {"ok": True, "status": "verified", "image_id": "sha256:" + "9" * 64},
    )

    def fake_probe(**values):
        candidate = gitlink_deletion_candidates(source)[0]
        verified = {
            **candidate,
            "base_mode": "160000",
            "base_type": "commit",
            "base_oid": oid,
            "probe_status": "verified",
        }
        return {
            "ok": True,
            "status": "verified",
            "verified": [verified],
            "probe": {
                "schema": probe.GITLINK_PROBE_SCHEMA,
                "status": "verified",
                "task": values["task"],
                "image": values["image"],
                "image_id": values["image_id"],
                "base_commit": values["base_commit"],
                "source_patch_sha256": values["source_patch_sha256"],
                "paths": [verified],
            },
        }

    monkeypatch.setattr(probe, "probe_gitlink_deletions", fake_probe)

    row = {
        "instance_id": "instance_internetarchive__openlibrary-1",
        "base_commit": "2" * 40,
    }
    prediction = {"instance_id": row["instance_id"], "model_patch": source}
    selection = probe.prepare_eval_patch_selection(
        row,
        prediction,
        {
            "audited_legacy_gitlink_evidence": {
                "schema": probe.LEGACY_GITLINK_AUDIT_SCHEMA,
                "audit_id": "legacy-probe-regression",
                "task": row["instance_id"],
                "base_commit": row["base_commit"],
                "source_patch_sha256": probe.patch_sha(source),
                "removed_gitlinks": [{"path": "vendor/infogami", "old_oid": oid}],
            }
        },
    )

    assert selection["ok"] is True
    assert selection["image_id"] == "sha256:" + "9" * 64
    assert selection["source_patch_sha256"] != selection["eval_patch_sha256"]
    assert "vendor/infogami" not in selection["model_patch"]
    assert "openlibrary/solr/query_utils.py" in selection["model_patch"]
    assert selection["filtered_patch_paths"] == [
        {
            "path": "vendor/infogami",
            "reason": "missing_snapshot_gitlink",
            "old_oid": oid,
            "base_oid": oid,
            "probe_status": "verified",
        }
    ]
    assert selection["gitlink_probe"]["task"] == "instance_internetarchive__openlibrary-1"
    assert selection["gitlink_probe"]["image"] == "registry.example/task:latest"
    assert selection["gitlink_probe"]["base_commit"] == "2" * 40
    assert (
        selection["gitlink_probe"]["source_patch_sha256"]
        == selection["source_patch_sha256"]
    )


def test_bind_eval_image_pins_ordinary_patch_to_immutable_id(monkeypatch) -> None:
    image = "registry.example/task:latest"
    image_id = "sha256:" + "8" * 64
    monkeypatch.setattr(probe, "image_for_row", lambda _row: image)
    monkeypatch.setattr(probe, "ensure_image", lambda value: {"ok": True, "image": value})
    monkeypatch.setattr(
        probe,
        "resolve_local_image_id",
        lambda value: {"ok": True, "status": "verified", "image_id": image_id},
    )
    selection = {
        "ok": True,
        "status": "ready",
        "model_patch": "diff --git a/a.py b/a.py\n+x = 1\n",
    }

    assert probe.bind_eval_image({"instance_id": "task-1"}, selection) is selection
    assert selection["image"] == image
    assert selection["image_id"] == image_id


def test_eval_patch_selection_keeps_intentional_gitlink_deletion(monkeypatch) -> None:
    oid = "1" * 40
    source = _gitlink_delete("vendor/intentional", oid)
    monkeypatch.setattr(probe, "current_generation_proof_valid", lambda *args: True)
    monkeypatch.setattr(
        probe,
        "ensure_image",
        lambda image: (_ for _ in ()).throw(AssertionError("probe must not run")),
    )

    selection = probe.prepare_eval_patch_selection(
        {"instance_id": "instance_org__repo-1", "base_commit": "2" * 40},
        {"model_patch": source},
        {
            "solver_git_snapshot": {
                "removed_gitlinks": [{"path": "vendor/intentional", "old_oid": oid}]
            }
        },
    )

    assert selection["ok"] is True
    assert selection["model_patch"] == source
    assert selection["gitlink_probe"] is None
    assert selection["filtered_patch_paths"] == []


def test_legacy_gitlink_filter_requires_explicit_bound_audit(monkeypatch) -> None:
    oid = "1" * 40
    source = _gitlink_delete("vendor/infogami", oid)
    source_sha = probe.patch_sha(source)
    row = {"instance_id": "instance_org__repo-1", "base_commit": "2" * 40}
    prediction = {"instance_id": row["instance_id"], "model_patch": source}
    audit = {
        "schema": probe.LEGACY_GITLINK_AUDIT_SCHEMA,
        "audit_id": "task41-manual-audit-20260713",
        "task": row["instance_id"],
        "base_commit": row["base_commit"],
        "source_patch_sha256": source_sha,
        "removed_gitlinks": [{"path": "vendor/infogami", "old_oid": oid}],
    }
    monkeypatch.setattr(probe, "current_generation_proof_valid", lambda *args: False)

    assert probe._trusted_removed_gitlinks(
        row,
        prediction,
        {"audited_legacy_gitlink_evidence": audit},
        source,
        source_sha,
    ) == {("vendor/infogami", oid)}
    forged = {**audit, "source_patch_sha256": "3" * 64}
    assert probe._trusted_removed_gitlinks(
        row,
        prediction,
        {"audited_legacy_gitlink_evidence": forged},
        source,
        source_sha,
    ) is None
