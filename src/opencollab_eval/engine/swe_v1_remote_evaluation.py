"""Official evaluation execution, summary, and remote CLI logic."""

# ruff: noqa: E501, F403, F405

from opencollab_eval.engine import swe_v1_remote_cleanup as remote_cleanup
from opencollab_eval.engine.swe_v1_candidate_go_dependencies import (
    candidate_added_go_modules,
)
from opencollab_eval.engine.swe_v1_remote_artifacts import *
from opencollab_eval.engine.swe_v1_remote_commands import *
from opencollab_eval.engine.swe_v1_remote_core import *
from opencollab_eval.engine.swe_v1_remote_eval_candidate import *
from opencollab_eval.engine.swe_v1_remote_eval_candidate import generation_readiness_failure
from opencollab_eval.engine.swe_v1_remote_eval_patch import *
from opencollab_eval.engine.swe_v1_remote_eval_retry import *
from opencollab_eval.engine.swe_v1_remote_eval_script import (
    direct_eval_script,
    eval_workspace_helper_sources,
)
from opencollab_eval.engine.swe_v1_remote_generation import *
from opencollab_eval.engine.swe_v1_remote_gitlink_probe import *
from opencollab_eval.engine.swe_v1_remote_pytest_controller import prolite_pytest_controller_source
from opencollab_eval.engine.swe_v1_remote_records import *
from opencollab_eval.engine.swe_v1_remote_runtime_dependencies import *
from opencollab_eval.engine.swe_v1_remote_state import *


def eval_for_task_once(row, patch_selection=None):
    task = row["instance_id"]
    run_dir = base_run_dir / task
    eval_dir = run_dir / eval_dir_name
    report_path = eval_dir / "reports" / task / "report.json"
    summary_path = eval_dir / "summary.json"
    done, prediction, metric, pairing = generation_done_for_mode(run_dir, task, eval_only=eval_only)
    if not done:
        if prediction is not None and metric is not None:
            original_model_patch = prediction_patch(prediction)
            model_patch = eval_model_patch(prediction)
            status = workflow_status(metric)
            if (
                original_model_patch.strip()
                and not model_patch.strip()
                and status in {"done", "done_with_timeout_patch"}
            ):
                summary = {
                    "schema": "opencollab.prolite_direct_eval.v2",
                    "status": "empty_eval_patch_invalid",
                    "task": task,
                    "resolved": False,
                    "patch_sha256": row_patch_sha(prediction),
                    "record_id": row_record_id(prediction),
                    "model_patch_chars": len(original_model_patch),
                    "eval_model_patch_chars": 0,
                    "technical_reasons": ["empty_eval_patch_after_filter"],
                    "pairing": pairing,
                }
                write_json(summary_path, summary)
                return {"status": "empty_eval_patch_invalid", "task": task, "summary": summary}
        return generation_readiness_failure(task, prediction, metric, pairing, require_identity=not eval_only)
    fail_to_pass = parse_literal_list(row.get("fail_to_pass") or row.get("FAIL_TO_PASS"))
    if not fail_to_pass:
        summary = {
            "schema": "opencollab.prolite_direct_eval.v2",
            "status": "blocked_missing_eval_spec",
            "task": task,
            "resolved": False,
            "patch_sha256": row_patch_sha(prediction),
            "record_id": row_record_id(prediction),
            "technical_reasons": ["missing_fail_to_pass"],
            "pairing": pairing,
        }
        write_json(summary_path, summary)
        return {"status": "blocked_missing_eval_spec", "task": task, "summary": summary}
    pass_to_pass = parse_literal_list(row.get("pass_to_pass") or row.get("PASS_TO_PASS"))
    candidate_source_paths = eval_candidate_source_paths(prediction)
    candidate_go_modules = candidate_added_go_modules(eval_model_patch(prediction))
    f2p_plan = prolite_test_plan(
        row,
        fail_to_pass,
        target_file="/eval_input/f2p.targets.json",
        candidate_source_paths=candidate_source_paths,
        candidate_added_go_modules=candidate_go_modules,
    )
    p2p_plan = prolite_test_plan(
        row,
        pass_to_pass,
        target_file="/eval_input/p2p.targets.json",
        candidate_source_paths=candidate_source_paths,
        candidate_added_go_modules=candidate_go_modules,
    )
    runtime_dependency_specs = plan_runtime_dependency_specs(f2p_plan, p2p_plan)
    eval_timeout = configured_eval_timeout = resolve_eval_timeout(globals().get("eval_timeout") or None)
    eval_spec_sha256 = prolite_eval_spec_sha256(
        row,
        f2p_plan,
        p2p_plan,
        eval_timeout=configured_eval_timeout,
        controller_timeout=configured_eval_timeout,
    )
    unverified_plan_reasons = []
    if not f2p_plan["coverage_verified"]:
        unverified_plan_reasons.append("no_verified_fail_to_pass_plan")
    if pass_to_pass and not p2p_plan["coverage_verified"]:
        unverified_plan_reasons.append("no_verified_pass_to_pass_plan")
    if unverified_plan_reasons:
        summary = {
            "schema": "opencollab.prolite_direct_eval.v2",
            "status": "technical_eval_failed",
            "task": task,
            "resolved": False,
            "patch_sha256": row_patch_sha(prediction),
            "record_id": row_record_id(prediction),
            "eval_spec_sha256": eval_spec_sha256,
            "technical_reasons": unverified_plan_reasons,
            "fail_to_pass_plan": f2p_plan,
            "pass_to_pass_plan": p2p_plan,
            "pairing": pairing,
            "executed": False,
        }
        write_json(summary_path, summary)
        return {
            "status": "technical_eval_failed",
            "task": task,
            "summary": summary,
            "executed": False,
        }
    prepared_patch = validated_eval_patch(
        row=row,
        prediction=prediction,
        metric=metric,
        pairing=pairing,
        eval_spec_sha256=eval_spec_sha256,
        summary_path=summary_path,
        patch_selection=patch_selection,
        runtime_dependency_specs=runtime_dependency_specs,
    )
    if not prepared_patch["ready"]:
        return prepared_patch["result"]
    patch_selection = prepared_patch["patch_selection"]
    model_patch = prepared_patch["model_patch"]
    patch_evidence = prepared_patch["patch_evidence"]
    previous = load_json(summary_path)
    if (
        isinstance(previous, dict)
        and previous.get("eval_spec_sha256") == eval_spec_sha256
        and eval_summary_matches_prediction(
            previous,
            prediction,
            task,
            eval_spec_sha256=eval_spec_sha256,
            f2p_plan=f2p_plan,
            p2p_plan=p2p_plan,
            expected_eval_patch_sha256=patch_selection["eval_patch_sha256"],
            expected_eval_image_id=str(patch_selection.get("image_id") or ""),
            expected_candidate_expectation=patch_selection.get("candidate_expectation"),
        )
    ):
        return {
            "status": "eval_done",
            "task": task,
            "summary": previous,
            "report_path": str(report_path),
            "eval_patch_sha256": patch_selection["eval_patch_sha256"],
        }
    if dry_run:
        return {
            "status": "would_eval",
            "task": task,
            "executed": False,
            "eval_patch_sha256": patch_selection["eval_patch_sha256"],
        }
    runtime_preparation = prepare_eval_runtime(
        row, prediction, patch_selection, runtime_dependency_specs, eval_spec_sha256, summary_path
    )
    if not runtime_preparation["ok"]:
        return runtime_preparation["result"]
    image, runtime_dependency_identities = runtime_preparation["image"], runtime_preparation["document"]
    input_dir = eval_dir / "input"
    reports_dir = eval_dir / "reports"
    output_dir = report_path.parent
    eval_dir.mkdir(parents=True, exist_ok=True)
    eval_dir.chmod(0o755)
    input_dir.mkdir(exist_ok=True)
    input_dir.chmod(0o755)
    reports_dir.mkdir(exist_ok=True)
    reports_dir.chmod(0o755)
    proof_nonce = uuid.uuid4().hex
    prepare_eval_output_directory(reports_dir, output_dir, task)
    original_model_patch = prediction_patch(prediction)
    test_patch = str(row.get("test_patch") or "")
    f2p_cmd = " && ".join(f2p_plan["commands"])
    p2p_cmd = " && ".join(p2p_plan["commands"])
    service_bootstrap = prolite_service_bootstrap(row)
    atomic_write_bytes(input_dir / "model.patch", model_patch.encode("utf-8"))
    atomic_write_bytes(input_dir / "test.patch", test_patch.encode("utf-8"))
    atomic_write_bytes(input_dir / "service_bootstrap.sh", service_bootstrap.encode("utf-8"))
    atomic_write_bytes(input_dir / "before_repo.sh", str(row.get("before_repo_set_cmd") or "").encode("utf-8"))
    atomic_write_bytes(
        input_dir / "base_commit",
        (str(row.get("base_commit") or row.get("commit") or "").strip() + "\n").encode("utf-8"),
    )
    for name, payload in eval_workspace_helper_sources().items():
        atomic_write_bytes(input_dir / name, payload)
    atomic_write_bytes(input_dir / "f2p.command", (f2p_cmd + "\n").encode("utf-8"))
    atomic_write_bytes(input_dir / "p2p.command", (p2p_cmd + "\n").encode("utf-8"))
    plan_inputs = {
        "candidate_expectation.json": patch_selection["candidate_expectation"],
        "f2p.targets.json": fail_to_pass,
        "p2p.targets.json": pass_to_pass,
        "runtime_dependency_specs.json": runtime_dependency_specs,
        "runtime_dependency_identities.json": runtime_dependency_identities,
    }
    for name, value in plan_inputs.items():
        write_json(input_dir / name, value)
    controller_path = input_dir / "opencollab_pytest_controller.py"
    atomic_write_bytes(input_dir / "opencollab_pytest_proof.py", prolite_pytest_proof_plugin_source().encode("utf-8"))
    atomic_write_bytes(controller_path, prolite_pytest_controller_source().encode("utf-8"))
    atomic_write_bytes(input_dir / "proof.nonce", (proof_nonce + "\n").encode("ascii"))
    atomic_write_bytes(
        input_dir / "f2p.sh",
        prolite_test_plan_script(
            f2p_plan,
            "f2p",
            proof_nonce,
            controller_timeout=configured_eval_timeout,
            shared_deadline_env="OPENCOLLAB_EVAL_DEADLINE",
        ).encode("utf-8"),
    )
    atomic_write_bytes(
        input_dir / "p2p.sh",
        prolite_test_plan_script(
            p2p_plan,
            "p2p",
            proof_nonce,
            controller_timeout=configured_eval_timeout,
            shared_deadline_env="OPENCOLLAB_EVAL_DEADLINE",
        ).encode("utf-8"),
    )
    write_json(input_dir / "f2p.plan.json", f2p_plan)
    write_json(input_dir / "p2p.plan.json", p2p_plan)
    inner = direct_eval_script()
    script_path = input_dir / "run_prolite_direct_eval.sh"
    atomic_write_bytes(script_path, inner.encode("utf-8"))
    for input_path in input_dir.iterdir():
        if input_path.is_file():
            input_path.chmod(0o644)
    script_path.chmod(0o755)
    controller_path.chmod(0o755)
    command_log = eval_dir / "command.log"
    cidfile = eval_dir / "container.cid"
    marker_path = eval_dir / "container.marker.json"
    previous_marker = load_json(marker_path)
    if isinstance(previous_marker, dict):
        previous_name = str(previous_marker.get("container_name") or "")
        stale_cleanup = cleanup_eval_container(
            cidfile,
            marker_path,
            previous_name,
        )
        if not stale_cleanup.get("ok"):
            summary = {
                "schema": "opencollab.prolite_direct_eval.v2",
                "status": "technical_eval_failed",
                "task": task,
                "resolved": False,
                "patch_sha256": row_patch_sha(prediction),
                "record_id": row_record_id(prediction),
                "technical_reasons": ["stale_container_cleanup"],
                "container_cleanup": stale_cleanup,
            }
            write_json(summary_path, summary)
            return {"status": "technical_eval_failed", "task": task, "summary": summary}
    elif marker_path.exists() or cidfile.exists():
        stale_cleanup = cleanup_eval_container(cidfile, marker_path, "")
        if not stale_cleanup.get("ok"):
            summary = {
                "schema": "opencollab.prolite_direct_eval.v2",
                "status": "technical_eval_failed",
                "task": task,
                "resolved": False,
                "patch_sha256": row_patch_sha(prediction),
                "record_id": row_record_id(prediction),
                "technical_reasons": ["stale_container_cleanup"],
                "container_cleanup": stale_cleanup,
            }
            write_json(summary_path, summary)
            return {"status": "technical_eval_failed", "task": task, "summary": summary}
    container_name = (
        "opencollab-prolite-"
        + hashlib.sha256(f"{base_run_dir}:{task}:{os.getpid()}:{time.time_ns()}".encode()).hexdigest()[:24]
    )
    temporary_output, container_output_dir = create_local_eval_output()
    cidfile.unlink(missing_ok=True)
    write_json(
        marker_path,
        {
            "schema": remote_cleanup.EVAL_CONTAINER_SCHEMA,
            "state": "pending",
            "task": task,
            "container_name": container_name,
            "container_id": "",
            "owner_nonce": owner_nonce,
            "owner_label": remote_cleanup.EVAL_OWNER_LABEL,
            "owner_schema_label": remote_cleanup.EVAL_SCHEMA_LABEL,
            "owner_schema": remote_cleanup.EVAL_SCHEMA_LABEL_VALUE,
            "cidfile": str(cidfile),
            "created_at": now(),
        },
    )
    timeout_prefix = ["timeout", str(eval_timeout)] if shutil.which("timeout") else []
    docker_cmd = [
        *timeout_prefix,
        "docker",
        "run",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,nodev,size=8g,mode=1777",
        "--rm",
        "--name",
        container_name,
        "--label",
        f"{remote_cleanup.EVAL_OWNER_LABEL}={owner_nonce}",
        "--label",
        f"{remote_cleanup.EVAL_SCHEMA_LABEL}={remote_cleanup.EVAL_SCHEMA_LABEL_VALUE}",
        "--network",
        "none",
        "--user",
        "0:0",
        "--entrypoint",
        "/bin/bash",
        "--cidfile",
        str(cidfile),
        "-v",
        f"{input_dir}:/eval_input:ro",
        "-v",
        f"{container_output_dir}:/eval_output",
        "--env",
        f"OPENCOLLAB_EVAL_TIMEOUT_SECONDS={configured_eval_timeout}",
        *_public_preparation_docker_env(),
        image,
        "/eval_input/run_prolite_direct_eval.sh",
    ]
    append_jsonl(
        run_dir / "eval_attempts.jsonl",
        {
            "time": now(),
            "phase": "eval_attempt_started",
            "task": task,
            "record_id": row_record_id(prediction),
            "patch_sha256": row_patch_sha(prediction),
            "eval_patch_sha256": patch_sha(model_patch),
            "eval_spec_sha256": eval_spec_sha256,
            "eval_image_id": str(patch_selection.get("image_id") or ""),
        },
    )
    cleanup_quiesced = True
    container_cleanup = None
    with open_locked_append(command_log) as log:
        log.write(("\n===== eval start " + now() + " =====\n").encode())
        spawn_signal_state = block_spawn_signals()
        try:
            proc = subprocess.Popen(
                docker_cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            try:
                restore_spawn_signals(spawn_signal_state)
            finally:
                container_cleanup = clear_pending_eval_marker(
                    cidfile,
                    marker_path,
                    container_name,
                )
            log.write((f"failed to start eval container: {exc}\n").encode())
            docker_exit = 127
        except BaseException:
            try:
                restore_spawn_signals(spawn_signal_state)
            finally:
                clear_pending_eval_marker(
                    cidfile,
                    marker_path,
                    container_name,
                )
                cleanup_temporary_output(temporary_output)
            raise
        else:
            ACTIVE_CHILD_PGIDS.add(proc.pid)
            try:
                binding = bind_eval_container_marker(
                    cidfile, marker_path, container_name, proc, timeout=eval_container_bind_timeout
                )
            except Exception as exc:
                binding = {
                    "ok": False,
                    "status": "container_identity_binding_exception",
                    "details": f"{type(exc).__name__}: {exc}",
                }
            except BaseException:
                cleanup_eval_binding_interruption(
                    proc,
                    cidfile,
                    marker_path,
                    container_name,
                    temporary_output,
                    spawn_signal_state,
                    cleanup_eval_container,
                    clear_pending_eval_marker,
                    cleanup_temporary_output,
                )
                raise
            if not binding.get("ok"):
                try:
                    cleanup_quiesced = terminate_process_group_bounded(proc)
                    cleanup = safe_eval_container_cleanup(cleanup_eval_container, cidfile, marker_path, container_name)
                    pending_cleanup = (
                        clear_pending_eval_marker(cidfile, marker_path, container_name)
                        if cleanup_quiesced and not cleanup.get("ok")
                        else None
                    )
                    cleanup = (
                        pending_cleanup if isinstance(pending_cleanup, dict) and pending_cleanup.get("ok") else cleanup
                    )
                    if cleanup_quiesced:
                        ACTIVE_CHILD_PGIDS.discard(proc.pid)
                    summary = {
                        "schema": "opencollab.prolite_direct_eval.v2",
                        "status": "technical_eval_failed",
                        "task": task,
                        "resolved": False,
                        "patch_sha256": row_patch_sha(prediction),
                        "record_id": row_record_id(prediction),
                        "technical_reasons": ["container_identity_binding"],
                        "container_binding": binding,
                        "container_cleanup": cleanup,
                        "cleanup_quiesced": cleanup_quiesced,
                    }
                    cleanup_errors = cleanup_temporary_output(temporary_output)
                    if cleanup_errors:
                        summary["technical_reasons"].append("temporary_output_cleanup")
                        summary["output_artifact_errors"] = cleanup_errors
                    write_json(summary_path, summary)
                    return {"status": "technical_eval_failed", "task": task, "summary": summary}
                finally:
                    restore_spawn_signals(spawn_signal_state)
            try:
                try:
                    restore_spawn_signals(spawn_signal_state)
                    docker_exit = proc.wait(timeout=eval_timeout + 120)
                    cleanup_quiesced = ensure_process_group_quiesced_after_wait(proc)
                    if not cleanup_quiesced:
                        docker_exit = PROCESS_CLEANUP_FAILED_EXIT_CODE
                except subprocess.TimeoutExpired:
                    log.write((f"outer eval timeout after {eval_timeout + 120}s\n").encode())
                    cleanup_quiesced = terminate_process_group_bounded(proc)
                    docker_exit = 124 if cleanup_quiesced else PROCESS_CLEANUP_FAILED_EXIT_CODE
                except BaseException:
                    cleanup_quiesced = False
                    try:
                        cleanup_quiesced = terminate_process_group_bounded(proc)
                    except BaseException:  # noqa: BLE001, S110 - preserve original failure
                        pass
                    try:
                        cleanup_eval_container(
                            cidfile,
                            marker_path,
                            container_name,
                        )
                    except BaseException:  # noqa: BLE001, S110 - preserve original failure
                        pass
                    cleanup_temporary_output(temporary_output)
                    raise
            finally:
                if cleanup_quiesced:
                    ACTIVE_CHILD_PGIDS.discard(proc.pid)

    if container_cleanup is None:
        container_cleanup = cleanup_eval_container(
            cidfile,
            marker_path,
            container_name,
        )
    artifacts = publish_and_read_eval_output_artifacts(
        container_output_dir,
        output_dir,
        f2p_plan,
        p2p_plan,
        proof_nonce,
        temporary_output,
        str(row.get("base_commit") or row.get("commit") or ""),
        runtime_dependency_identities,
        str(patch_selection.get("image_id") or ""),
        patch_selection["candidate_expectation"],
    )
    verdict = derive_eval_verdict(
        artifacts, docker_exit=docker_exit, cleanup_quiesced=cleanup_quiesced, container_cleanup=container_cleanup
    )
    output_artifact_errors = verdict["output_artifact_errors"]
    diagnostic_artifact_errors = artifacts["diagnostic_artifact_errors"]
    base_commit_status = artifacts["base_commit_status"]
    service_status = artifacts["service_status"]
    before_status = artifacts["before_status"]
    post_before_base_status = artifacts["post_before_base_status"]
    model_status = artifacts["model_status"]
    test_status = artifacts["test_status"]
    f2p_status = artifacts["f2p_status"]
    p2p_status = artifacts["p2p_status"]
    f2p_log_tail = artifacts["f2p_log_tail"]
    p2p_log_tail = artifacts["p2p_log_tail"]
    f2p_evidence = artifacts["f2p_evidence"]
    p2p_evidence = artifacts["p2p_evidence"]
    f2p_command = artifacts["f2p_command"]
    p2p_command = artifacts["p2p_command"]
    base_commit_log_tail = artifacts["base_commit_log_tail"]
    before_repo_log_tail = artifacts["before_repo_log_tail"]
    service_bootstrap_log_tail = artifacts["service_bootstrap_log_tail"]
    model_patch_log_tail = artifacts["model_patch_log_tail"]
    test_patch_log_tail = artifacts["test_patch_log_tail"]
    technical_reasons = verdict["technical_reasons"]
    technical_error, resolved, summary_status = (
        verdict["technical_error"],
        verdict["resolved"],
        verdict["summary_status"],
    )
    outcome_fields = {key: verdict[key] for key in ("outcome", "outcome_basis", "operational_warnings")}
    report = {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": summary_status,
        "instance_id": task,
        "resolved": resolved,
        **outcome_fields,
        "patch_successfully_applied": model_status == 0,
        "error": bool(technical_error),
        "technical_reasons": technical_reasons,
        "output_artifact_errors": output_artifact_errors,
        "diagnostic_artifact_errors": diagnostic_artifact_errors,
        "docker_exit": docker_exit,
        "cleanup_quiesced": cleanup_quiesced,
        "container_cleanup": container_cleanup,
        "patch_sha256": row_patch_sha(prediction),
        "base_snapshot_integrity": artifacts["base_snapshot"],
        "candidate_projection_failure": artifacts["candidate_projection_failure"],
        "candidate_projection": artifacts["candidate_projection"],
        "source_candidate_projection": artifacts["source_candidate_projection"],
        "runtime_dependencies": artifacts["runtime_dependencies"],
        "runtime_dependency_identities": runtime_dependency_identities,
        **patch_evidence,
        "record_id": row_record_id(prediction),
        "eval_spec_sha256": eval_spec_sha256,
        "model_patch_chars": len(original_model_patch),
        "eval_model_patch_chars": len(model_patch),
        "tests_status": {
            "base_commit_status": base_commit_status,
            "service_bootstrap_status": service_status,
            "before_repo_status": before_status,
            "post_before_base_status": post_before_base_status,
            "model_patch_status": model_status,
            "test_patch_status": test_status,
            "fail_to_pass_status": f2p_status,
            "pass_to_pass_status": p2p_status,
            "fail_to_pass": fail_to_pass,
            "pass_to_pass": pass_to_pass,
            "fail_to_pass_plan": f2p_plan,
            "pass_to_pass_plan": p2p_plan,
            "fail_to_pass_evidence": f2p_evidence,
            "pass_to_pass_evidence": p2p_evidence,
            "f2p_command": f2p_command,
            "p2p_command": p2p_command,
            "base_commit_log_tail": base_commit_log_tail,
            "before_repo_log_tail": before_repo_log_tail,
            "service_bootstrap_log_tail": service_bootstrap_log_tail,
            "f2p_log_tail": f2p_log_tail,
            "p2p_log_tail": p2p_log_tail,
            "model_patch_log_tail": model_patch_log_tail,
            "test_patch_log_tail": test_patch_log_tail,
        },
    }
    write_json(report_path, {task: report})
    summary = {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": summary_status,
        "task": task,
        "resolved": resolved,
        **outcome_fields,
        "patch_sha256": row_patch_sha(prediction),
        **patch_evidence,
        "record_id": row_record_id(prediction),
        "eval_spec_sha256": eval_spec_sha256,
        "model_patch_chars": len(original_model_patch),
        "eval_model_patch_chars": len(model_patch),
        "technical_reasons": technical_reasons,
        "output_artifact_errors": output_artifact_errors,
        "diagnostic_artifact_errors": diagnostic_artifact_errors,
        "docker_exit": docker_exit,
        "cleanup_quiesced": cleanup_quiesced,
        "container_cleanup": container_cleanup,
        "report_path": str(report_path),
        "command_log": str(command_log),
        "base_snapshot_integrity": artifacts["base_snapshot"],
        "runtime_dependencies": artifacts["runtime_dependencies"],
        "candidate_projection_failure": artifacts["candidate_projection_failure"],
        "candidate_projection": artifacts["candidate_projection"],
        "source_candidate_projection": artifacts["source_candidate_projection"],
        "runtime_dependency_identities": runtime_dependency_identities,
        "tests_status": report["tests_status"],
    }
    write_json(summary_path, summary)
    return {
        "status": "eval_done" if not technical_error else "technical_eval_failed",
        "task": task,
        "summary": summary,
        "report_path": str(report_path),
        "executed": True,
        "eval_patch_sha256": patch_selection["eval_patch_sha256"],
    }


def eval_for_task(row):
    return eval_for_task_with_retries(
        row,
        eval_for_task_once,
        resolve_eval_timeout(globals().get("eval_timeout") or None),
        resolve_eval_timeout(globals().get("eval_timeout") or None),
    )


from opencollab_eval.engine.swe_v1_remote_execution import (  # noqa: E402, F401
    main,
    task_outcome,
    write_markdown,
)
