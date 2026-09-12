"""Remote task execution and result reporting."""

# ruff: noqa: E501, F403, F405

from opencollab_eval.engine.swe_v1_remote_artifacts import *
from opencollab_eval.engine.swe_v1_remote_commands import *
from opencollab_eval.engine.swe_v1_remote_core import *
from opencollab_eval.engine.swe_v1_remote_eval_candidate import *
from opencollab_eval.engine.swe_v1_remote_eval_only import (
    candidate_isolation_summary,
    prepare_eval_only_candidate_isolation,
)
from opencollab_eval.engine.swe_v1_remote_eval_patch import *
from opencollab_eval.engine.swe_v1_remote_eval_retry import *
from opencollab_eval.engine.swe_v1_remote_generation import *
from opencollab_eval.engine.swe_v1_remote_gitlink_probe import *
from opencollab_eval.engine.swe_v1_remote_health import http_health
from opencollab_eval.engine.swe_v1_remote_records import *
from opencollab_eval.engine.swe_v1_remote_runtime_dependencies import *
from opencollab_eval.engine.swe_v1_remote_state import *


def task_outcome(gen, ev):
    """Report capability outcome while preserving the original official summary."""
    official = ev.get("summary") or {}
    candidate_resolved = official.get("resolved") if ev.get("status") == "eval_done" else None
    intrinsic = gen.get("oc_failure") is True
    technical = gen.get("technical_failure") is True or (
        gen.get("status")
        not in {
            "generation_done",
            "generation_candidate_captured",
            "empty_patch",
            "intrinsic_unresolved",
            "would_generate",
        }
        or ev.get("status") not in {"eval_done", "skipped_empty_patch", "skipped_intrinsic_unresolved", "would_eval"}
    )
    status = (
        "resolved"
        if candidate_resolved is True
        else "unresolved"
        if intrinsic
        else "technical_failure"
        if technical
        else "unresolved"
    )
    return dict(
        status=status,
        resolved=status == "resolved",
        oc_failure=intrinsic,
        candidate_official_resolved=candidate_resolved,
        technical_failure=technical and not intrinsic,
        candidate_evaluation_technical_failure=technical and intrinsic,
        failure_origin=gen.get("failure_origin"),
        agent_status=gen.get("agent_status"),
        agent_reason=gen.get("agent_reason"),
        origin_record_id=gen.get("origin_record_id"),
    )


def write_markdown(summary):
    lines = [
        f"# SWE G1.1 Pro-Lite {summary.get('slice', slice_label())} Report",
        "",
        f"- generated_at: `{summary['generated_at']}`",
        f"- base_run_dir: `{summary['base_run_dir']}`",
        f"- remote_runtime_repo: `{summary['remote_runtime_repo']}`",
        f"- workflow: `{summary['workflow']}`",
        f"- solver_attribution: `{summary['solver_attribution']}`",
        f"- llm_model: `{summary['llm_model']}`",
        f"- tasks: `{summary['counts']['tasks']}`",
        f"- generation_done: `{summary['counts']['generation_done']}`",
        f"- eval_done: `{summary['counts']['eval_done']}`",
        f"- resolved: `{summary['counts']['resolved']}`",
        f"- unresolved: `{summary['counts']['unresolved']}`",
        f"- technical_failed: `{summary['counts']['technical_failed']}`",
        "",
        "| idx | task | generation | eval | resolved | patch | report |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in summary["rows"]:
        report = row.get("eval", {}).get("report_path") or ""
        patch_sha = (
            row.get("generation", {}).get("patch_sha256")
            or (row.get("eval", {}).get("summary") or {}).get("patch_sha256")
            or ""
        )
        lines.append(
            "| {idx} | `{task}` | `{gen}` | `{ev}` | `{resolved}` | `{patch}` | `{report}` |".format(
                idx=row["index"],
                task=row["task"],
                gen=row.get("generation", {}).get("status", ""),
                ev=row.get("eval", {}).get("status", ""),
                resolved=(row.get("task_result") or {}).get("resolved", ""),
                patch=patch_sha[:12],
                report=report,
            )
        )
    summary["markdown"] = "\n".join(lines) + "\n"


def main():
    config_errors = validate_runner_config()
    if config_errors:
        summary = {
            "schema": "opencollab.swe_g11_prolite_runner.v1",
            "status": "invalid_config",
            "generated_at": now(),
            "slice": slice_label(),
            "base_run_dir": str(base_run_dir),
            "remote_runtime_repo": str(remote_repo),
            "workflow": workflow,
            "config_errors": config_errors,
            "counts": {
                "tasks": 0,
                "generation_done": 0,
                "empty_patch": 0,
                "eval_done": 0,
                "eval_attempts": 0,
                "eval_retry_tasks": 0,
                "resolved": 0,
                "unresolved": 0,
                "technical_failed": 1,
            },
            "rows": [],
        }
        write_json(base_run_dir / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 2
    preflight = {
        "dataset_exists": dataset_path.exists(),
        "remote_root_exists": remote_root.exists(),
        "remote_repo_exists": remote_repo.exists(),
        "remote_runtime_required": not eval_only,
        "proxy_health": (
            {"ok": True, "status": "skipped_eval_only"}
            if eval_only
            else (
                {"ok": True, "status": "not_applicable_direct"}
                if llm_transport == "direct"
                else http_health(proxy_health_url(remote_proxy_base_url), timeout=45)
            )
        ),
    }
    if not all(
        [
            preflight["dataset_exists"],
            preflight["remote_root_exists"],
            preflight["remote_repo_exists"] or not preflight["remote_runtime_required"],
            preflight["proxy_health"].get("ok"),
        ]
    ):
        summary = {
            "schema": "opencollab.swe_g11_prolite_runner.v1",
            "status": "preflight_failed",
            "generated_at": now(),
            "slice": slice_label(),
            "base_run_dir": str(base_run_dir),
            "remote_runtime_repo": str(remote_repo),
            "workflow": workflow,
            "preflight": preflight,
            "counts": {
                "tasks": 0,
                "generation_done": 0,
                "empty_patch": 0,
                "eval_done": 0,
                "eval_attempts": 0,
                "eval_retry_tasks": 0,
                "resolved": 0,
                "unresolved": 0,
                "technical_failed": 1,
            },
            "rows": [],
        }
        write_json(base_run_dir / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 2
    selected = load_dataset(start_index, limit)
    base_run_dir.mkdir(parents=True, exist_ok=True)
    candidate_isolation = prepare_eval_only_candidate_isolation(selected)
    result_rows = []
    for offset, row in enumerate(selected, start_index):
        task = row["instance_id"]
        if eval_only:
            run_dir = base_run_dir / task
            done, prediction, metric, pairing = generation_done_for_mode(run_dir, task, eval_only=True)
            if done:
                matching_attempts = _matching_official_eval_attempt_count(run_dir, task)
                identity_status = eval_only_generation_identity_status(
                    prediction, metric, task, matching_official_eval_attempts=matching_attempts
                )
                gen = reconcile_eval_only_candidate_identity(
                    generation_done_result(
                        task,
                        prediction,
                        metric,
                        pairing,
                        eval_only=True,
                        artifact_identity_status=identity_status,
                    )
                )
            else:
                gen = {
                    "status": "skipped_no_generation_patch",
                    "task": task,
                    "pairing": pairing,
                    "eval_only": True,
                }
            generation_phase = "generation_observed"
        else:
            gen = generation_for_task(row)
            generation_phase = "generation"
        append_jsonl(
            base_run_dir / "events.jsonl", {"time": now(), "phase": generation_phase, "task": task, "result": gen}
        )
        if gen.get("status") == "intrinsic_unresolved":
            ev = {
                "status": "skipped_intrinsic_unresolved",
                "task": task,
                "attempt_count": 0,
                "max_eval_attempts": max_eval_attempts,
            }
        elif gen.get("status") == "empty_patch":
            ev = {
                "status": "skipped_empty_patch",
                "task": task,
                "pairing": gen.get("pairing"),
                "attempt_count": 0,
                "max_eval_attempts": max_eval_attempts,
            }
        elif dry_run and gen.get("status") in {"would_generate", "generation_done"}:
            ev = {"status": "would_eval", "task": task}
        elif gen.get("status") in {"generation_done", "generation_candidate_captured"}:
            ev = eval_for_task(row)
        else:
            ev = {
                "status": "skipped_generation_not_ready",
                "task": task,
                "generation_status": gen.get("status"),
                "reason": "generation_not_ready",
            }
        append_jsonl(base_run_dir / "events.jsonl", {"time": now(), "phase": "eval", "task": task, "result": ev})
        outcome = task_outcome(gen, ev)
        result_rows.append(
            {
                "index": offset,
                "task": task,
                "generation": gen,
                "eval": ev,
                "task_result": outcome,
                "oc_failure": outcome["oc_failure"],
                "candidate_official_resolved": outcome["candidate_official_resolved"],
            }
        )
    generation_ok_statuses = {"generation_done", "generation_candidate_captured", "empty_patch", "intrinsic_unresolved"}
    eval_ok_statuses = {"eval_done", "skipped_empty_patch", "skipped_intrinsic_unresolved"}
    if dry_run:
        generation_ok_statuses.add("would_generate")
        eval_ok_statuses.add("would_eval")
    counts = {
        "tasks": len(result_rows),
        "generation_done": sum(1 for row in result_rows if row["generation"].get("status") == "generation_done"),
        "generation_candidate_captured": sum(
            1 for row in result_rows if row["generation"].get("status") == "generation_candidate_captured"
        ),
        "empty_patch": sum(1 for row in result_rows if row["generation"].get("status") == "empty_patch"),
        "would_generate": sum(1 for row in result_rows if row["generation"].get("status") == "would_generate"),
        "eval_done": sum(1 for row in result_rows if row["eval"].get("status") == "eval_done"),
        "would_eval": sum(1 for row in result_rows if row["eval"].get("status") == "would_eval"),
        **eval_attempt_summary(result_rows),
        "candidate_official_resolved": sum(1 for row in result_rows if row["candidate_official_resolved"] is True),
        "oc_failure": sum(1 for row in result_rows if row["oc_failure"]),
        "resolved": sum(1 for row in result_rows if row["task_result"]["status"] == "resolved"),
        "unresolved": sum(1 for row in result_rows if row["task_result"]["status"] == "unresolved"),
        "technical_failed": sum(1 for row in result_rows if row["task_result"]["technical_failure"]),
    }
    status = "done" if counts["technical_failed"] == 0 else "done_with_technical_failures"
    if dry_run and counts["technical_failed"] == 0:
        status = "dry_run"
    summary = {
        "schema": "opencollab.swe_g11_prolite_runner.v1",
        "status": status,
        "generated_at": now(),
        "slice": slice_label(),
        "base_run_dir": str(base_run_dir),
        "remote_runtime_repo": str(remote_repo),
        "remote_python": str(cfg.get("remote_python") or "python3"),
        "workflow": workflow,
        "workflow_env": workflow_env,
        "openhands_command_sha256": openhands_command_sha256,
        "openhands_empty_patch_rejections": openhands_empty_patch_rejections,
        "max_empty_patch_retries": max_empty_patch_retries,
        "model_name": model_name,
        "llm_model": llm_model,
        "llm_provider": llm_provider,
        "llm_transport": llm_transport,
        "context_window": context_window,
        "temperature": temperature,
        "top_p": top_p,
        "max_output_tokens": max_output_tokens,
        "invocation_id": invocation_id,
        "run_id": run_id,
        "runtime_tree_sha256": runtime_tree_sha256,
        "budget": budget,
        "max_steps": max_steps,
        "max_task_starts": max_task_starts,
        "max_eval_attempts": max_eval_attempts,
        "eval_only": eval_only,
        **candidate_isolation_summary(candidate_isolation),
        "eval_dir_name": eval_dir_name,
        "solver_attribution": "historical_artifact" if eval_only else "current_run",
        "preflight": preflight,
        "counts": counts,
        "rows": result_rows,
        "failure_scope": result_failure_scope(result_rows, counts["technical_failed"]),
    }
    write_markdown(summary)
    write_json(base_run_dir / "summary.json", summary)
    atomic_write_bytes(base_run_dir / "summary.md", summary["markdown"].encode("utf-8"))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if counts["technical_failed"] == 0 else 1


__all__ = [name for name in globals() if not name.startswith("__")]
