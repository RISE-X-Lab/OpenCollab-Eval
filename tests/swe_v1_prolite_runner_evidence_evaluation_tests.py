"""Official evaluation plan and execution evidence tests."""

from __future__ import annotations

from swe_v1_prolite_runner_test_support import (
    Path,
    _remote_namespace,
    _seed_remote_completed_generation,
    _test_only_patch,
    _write_jsonl,
    json,
    pytest,
)


def test_prolite_prediction_sha_comes_from_patch_text(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    stale_patch = "diff --git a/src/a.py b/src/a.py\n+stale\n"
    current_patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    stale_sha = namespace["patch_sha"](stale_patch)
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": stale_sha,
        "model_patch": current_patch,
    }
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": stale_sha,
        "workflow_status": "done",
        "runner_returncode": 0,
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    _write_jsonl(run_dir / "metrics.jsonl", [metric])

    done, _prediction, _metric, pairing = namespace["generation_done"](run_dir, task)

    assert namespace["row_patch_sha"](prediction) == namespace["patch_sha"](current_patch)
    assert done is False
    assert pairing == "record_id_patch_sha_mismatch"


def test_remote_patch_sha_match_requires_exact_hex_digest(tmp_path):
    namespace = _remote_namespace(tmp_path)
    digest = "a1" * 32

    assert namespace["patch_sha_matches"](digest, digest) is True
    assert namespace["patch_sha_matches"](digest[:12], digest) is False
    assert namespace["patch_sha_matches"]("g" * 64, "g" * 64) is False
    assert namespace["patch_sha_matches"](digest.upper(), digest) is False


def test_prolite_python_plan_batches_81_parameters_by_parent_without_file_fallback(tmp_path):
    namespace = _remote_namespace(tmp_path)
    targets = [f"tests/test_many.py::test_case[{index}]" for index in range(81)]

    plan = namespace["prolite_test_plan"]({"repo_language": "python"}, targets)

    assert plan["coverage_verified"] is True
    assert plan["coverage"] == "parameter_parent_targets"
    assert plan["target_batches"] == [targets]
    assert plan["commands"] == [
        "pytest -p opencollab_pytest_proof -q -rA -o addopts= "
        "tests/test_many.py::test_case"
    ]
    assert plan["proofs"][0]["parameter_fallback_parents"] == [
        "tests/test_many.py::test_case"
    ]


@pytest.mark.parametrize(
    "evidence_mode",
    ["matching", "tampered", "missing_log", "unsafe_late_log", "go_package_pass_only"],
)
def test_prolite_eval_requires_matching_batch_and_target_evidence(
    monkeypatch,
    tmp_path,
    evidence_mode,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-81"
    container_id = "d" * 64
    is_go = evidence_mode == "go_package_pass_only"
    targets = (
        ["internal/api/widget_test.go::TestWidget"]
        if is_go
        else [f"tests/test_many.py::test_case[{index}]" for index in range(81)]
    )
    _seed_remote_completed_generation(namespace, task)

    class FinishedProcess:
        pid = 424280

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, *args, **kwargs):
        input_mount = next(item for item in command if str(item).endswith(":/eval_input:ro"))
        output_mount = next(item for item in command if str(item).endswith(":/eval_output"))
        input_dir = Path(str(input_mount).removesuffix(":/eval_input:ro"))
        output_dir = Path(str(output_mount).removesuffix(":/eval_output"))
        cidfile = Path(command[command.index("--cidfile") + 1])
        cidfile.write_text(container_id, encoding="ascii")
        proof_nonce = (input_dir / "proof.nonce").read_text(encoding="ascii").strip()
        for name in (
            "base_commit.exit",
            "service_bootstrap.exit",
            "before_repo.exit",
            "post_before_base.exit",
            "model_patch.exit",
            "test_patch.exit",
            "f2p.exit",
            "p2p.exit",
        ):
            (output_dir / name).write_text("0\n", encoding="ascii")
        for name in ("service_bootstrap.log", "model_patch.log", "test_patch.log"):
            (output_dir / name).write_text("", encoding="utf-8")
        if evidence_mode == "unsafe_late_log":
            unsafe = output_dir / "service_bootstrap.log"
            unsafe.unlink()
            unsafe.symlink_to(output_dir / "attacker.log")
        for prefix in ("f2p", "p2p"):
            (output_dir / f"{prefix}.command").write_bytes((input_dir / f"{prefix}.command").read_bytes())
            (output_dir / f"{prefix}.log").write_text("", encoding="utf-8")
            plan = json.loads((input_dir / f"{prefix}.plan.json").read_text(encoding="utf-8"))
            for index, batch_command in enumerate(plan["commands"], 1):
                stem = output_dir / f"{prefix}.batch_{index:03d}"
                observed_command = (
                    "echo ok"
                    if evidence_mode == "tampered" and prefix == "f2p" and index == len(plan["commands"])
                    else batch_command
                )
                Path(f"{stem}.command").write_text(observed_command + "\n", encoding="utf-8")
                Path(f"{stem}.exit").write_text("0\n", encoding="ascii")
                if not (
                    evidence_mode == "missing_log"
                    and prefix == "f2p"
                    and index == len(plan["commands"])
                ):
                    batch_log = (
                        '{"Action":"pass","Package":"example/internal/api"}\n'
                        if is_go and prefix == "f2p"
                        else "".join(
                            f"PASSED {target}\n"
                            for target in plan["target_batches"][index - 1]
                        )
                        + f"{len(plan['target_batches'][index - 1])} passed in 0.01s\n"
                    )
                    Path(f"{stem}.log").write_text(batch_log, encoding="utf-8")
                proof = plan["proofs"][index - 1]
                if proof.get("kind") == "pytest_structured_reports":
                    nodes = plan["target_batches"][index - 1]
                    events = [
                        {"event": "session_start"},
                        {"event": "collection_finish", "nodeids": nodes},
                    ]
                    for node in nodes:
                        events.extend(
                            {
                                "event": "runtest_logreport",
                                "nodeid": node,
                                "when": phase,
                                "outcome": "passed",
                            }
                            for phase in ("setup", "call", "teardown")
                        )
                    events.append({"event": "session_finish", "exitstatus": 0})
                    proof_path = Path(f"{stem}.proof.{proof_nonce}.jsonl")
                    proof_path.write_text(
                        "".join(json.dumps(event) + "\n" for event in events),
                        encoding="utf-8",
                    )
        return FinishedProcess()

    inspect_calls = 0

    def fake_run(command, timeout=60):
        nonlocal inspect_calls
        if command[1] == "inspect":
            inspect_calls += 1
            if inspect_calls > 1:
                return {"returncode": 1, "stdout": "", "stderr": "No such container"}
            return {
                "returncode": 0,
                "stdout": (f"{container_id}\t{namespace['owner_nonce']}\tdirect-eval-v1"),
                "stderr": "",
            }
        return {"returncode": 0, "stdout": container_id, "stderr": ""}

    monkeypatch.setattr(namespace["subprocess"], "Popen", fake_popen)
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["ensure_process_group_quiesced_after_wait"] = lambda proc: True
    namespace["cleanup_eval_container"] = lambda *args, **kwargs: {
        "ok": True,
        "status": "all_references_absent",
    }
    namespace["run"] = fake_run

    result = namespace["eval_for_task"](
        {
            "instance_id": task,
            "fail_to_pass": targets,
            "repo_language": "go" if is_go else "python",
        }
    )

    evidence = result["summary"]["tests_status"]["fail_to_pass_evidence"]
    assert len(evidence) == 1
    if evidence_mode == "go_package_pass_only":
        assert result["status"] == "technical_eval_failed"
        assert result["summary"]["resolved"] is False
        assert "fail_to_pass_evidence" in result["summary"]["technical_reasons"]
        assert evidence[-1]["target_proof_matches_plan"] is False
    elif evidence_mode == "tampered":
        assert result["status"] == "technical_eval_failed"
        assert result["summary"]["resolved"] is False
        assert "fail_to_pass_evidence" in result["summary"]["technical_reasons"]
        assert evidence[-1]["command_matches_plan"] is False
    elif evidence_mode == "missing_log":
        assert result["status"] == "technical_eval_failed"
        assert result["summary"]["resolved"] is False
        assert "fail_to_pass_evidence" in result["summary"]["technical_reasons"]
        assert evidence[-1]["log_artifact_safe"] is False
    elif evidence_mode == "unsafe_late_log":
        assert result["status"] == "technical_eval_failed"
        assert result["summary"]["resolved"] is False
        assert "unsafe_or_missing_output_artifact" in result["summary"]["technical_reasons"]
        assert any(
            error.startswith("unsafe:service_bootstrap.log")
            for error in result["summary"]["output_artifact_errors"]
        )
    else:
        assert result["status"] == "eval_done"
        assert result["summary"]["resolved"] is True
        assert all(
            item["command_matches_plan"] and item["log_artifact_safe"] and item["artifact_safe"]
            for item in evidence
        )
        namespace["ensure_image"] = lambda image: pytest.fail(
            "valid persisted evidence should be reused before Docker"
        )
        reused = namespace["eval_for_task"](
            {
                "instance_id": task,
                "fail_to_pass": targets,
                "repo_language": "python",
            }
        )
        assert reused["status"] == "eval_done"
        assert reused["summary"]["resolved"] is True


def test_prolite_eval_marks_ruby_echo_ok_as_technical_red(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "ruby-task"
    _seed_remote_completed_generation(namespace, task)
    namespace["ensure_image"] = lambda image: pytest.fail("unverified commands must fail before Docker")

    result = namespace["eval_for_task"](
        {
            "instance_id": task,
            "fail_to_pass": ["spec/widget_spec.rb"],
            "repo_language": "ruby",
            "test_cmd": "echo ok",
            "eval_cmd": "echo ok",
        }
    )

    assert result["status"] == "technical_eval_failed"
    assert result["summary"]["resolved"] is False
    assert result["summary"]["technical_reasons"] == ["no_verified_fail_to_pass_plan"]


def test_remote_runner_does_not_reuse_stale_done_for_test_only_patch(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    patch = _test_only_patch()
    patch_sha = namespace["patch_sha"](patch)
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
    }
    stale_summary = {
        "status": "done",
        "task": task,
        "patch_sha256": patch_sha,
        "record_id": "r1",
        "resolved": True,
    }

    assert namespace["eval_summary_matches_prediction"](stale_summary, prediction, task) is False


def test_remote_runner_rejects_identity_only_done_summary_without_test_evidence(
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": namespace["patch_sha"](patch),
        "model_patch": patch,
    }
    f2p_plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        ["tests/test_x.py::test_target"],
    )
    p2p_plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        [],
    )
    eval_spec_sha256 = namespace["prolite_eval_spec_sha256"](
        {},
        f2p_plan,
        p2p_plan,
    )
    identity_only = {
        "status": "done",
        "task": task,
        "patch_sha256": prediction["patch_sha256"],
        "record_id": "r1",
        "eval_spec_sha256": eval_spec_sha256,
        "resolved": True,
    }

    assert namespace["eval_summary_matches_prediction"](
        identity_only,
        prediction,
        task,
        eval_spec_sha256=eval_spec_sha256,
        f2p_plan=f2p_plan,
        p2p_plan=p2p_plan,
    ) is False
