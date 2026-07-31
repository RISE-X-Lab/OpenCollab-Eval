"""Prepare trustworthy clean-child patches for direct evaluation."""

# ruff: noqa: F403, F405

import re

from opencollab_eval.engine.swe_v1_remote_commands import *
from opencollab_eval.engine.swe_v1_remote_gitlink_probe import *
from opencollab_eval.engine.swe_v1_remote_records import *
from opencollab_eval.engine.swe_v1_remote_state import *

_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _candidate_expectation_valid(expectation, row, prediction, patch_selection):
    if not isinstance(expectation, dict):
        return False
    required = {
        "schema",
        "instance_id",
        "record_id",
        "run_identity_sha256",
        "source_patch_sha256",
        "eval_patch_sha256",
        "source_base_commit",
        "source_anonymous_base",
        "source_base_tree",
        "source_candidate_tree",
        "expected_candidate_tree",
    }
    return bool(
        set(expectation) == required
        and expectation.get("schema") == "opencollab.eval_candidate_expectation.v1"
        and expectation.get("instance_id") == str(row.get("instance_id") or "")
        and expectation.get("record_id") == row_record_id(prediction)
        and expectation.get("record_id")
        and re.fullmatch(r"[0-9a-f]{64}", str(expectation.get("run_identity_sha256") or ""))
        and expectation.get("source_patch_sha256") == patch_selection.get("source_patch_sha256")
        and expectation.get("eval_patch_sha256") == patch_selection.get("eval_patch_sha256")
        and all(
            isinstance(expectation.get(key), str)
            and (
                not expectation[key]
                or _OBJECT_ID_RE.fullmatch(expectation[key]) is not None
            )
            for key in (
                "source_base_commit",
                "source_anonymous_base",
                "source_base_tree",
                "source_candidate_tree",
                "expected_candidate_tree",
            )
        )
        and len({bool(expectation[key]) for key in (
            "source_base_commit", "source_anonymous_base", "source_base_tree"
        )}) == 1
        and (
            not expectation["expected_candidate_tree"]
            or expectation["expected_candidate_tree"] == expectation["source_candidate_tree"]
        )
    )


def verified_plan_patch_selection(row, prediction, metric):
    fail_to_pass = parse_literal_list(row.get("fail_to_pass") or row.get("FAIL_TO_PASS"))
    if not fail_to_pass:
        return None
    pass_to_pass = parse_literal_list(row.get("pass_to_pass") or row.get("PASS_TO_PASS"))
    candidate_source_paths = eval_candidate_source_paths(prediction)
    f2p_plan = prolite_test_plan(
        row,
        fail_to_pass,
        target_file="/eval_input/f2p.targets.json",
        candidate_source_paths=candidate_source_paths,
    )
    p2p_plan = prolite_test_plan(
        row,
        pass_to_pass,
        target_file="/eval_input/p2p.targets.json",
        candidate_source_paths=candidate_source_paths,
    )
    if not f2p_plan["coverage_verified"] or (
        pass_to_pass and not p2p_plan["coverage_verified"]
    ):
        return None
    selection = bind_eval_image(
        row,
        prepare_eval_patch_selection(
            row,
            prediction,
            metric,
            plan_runtime_dependency_specs(f2p_plan, p2p_plan),
        ),
    )
    selection["eval_spec_sha256"] = prolite_eval_spec_sha256(row, f2p_plan, p2p_plan)
    return selection


def validated_eval_patch(
    *,
    row,
    prediction,
    metric,
    pairing,
    eval_spec_sha256,
    summary_path,
    patch_selection=None,
    runtime_dependency_specs=(),
):
    task = row["instance_id"]
    if patch_selection is None:
        patch_selection = prepare_eval_patch_selection(
            row, prediction, metric, runtime_dependency_specs
        )
        patch_selection["eval_spec_sha256"] = eval_spec_sha256
    selection_eval_spec_sha256 = str(patch_selection.get("eval_spec_sha256") or "")
    if (
        re.fullmatch(r"[0-9a-f]{64}", selection_eval_spec_sha256) is None
        or selection_eval_spec_sha256 != eval_spec_sha256
    ):
        summary = {
            "schema": "opencollab.prolite_direct_eval.v2",
            "status": "technical_eval_failed",
            "task": task,
            "resolved": False,
            "patch_sha256": row_patch_sha(prediction),
            "record_id": row_record_id(prediction),
            "eval_spec_sha256": eval_spec_sha256,
            "technical_reasons": ["eval_spec_identity_mismatch"],
            "pairing": pairing,
            "executed": False,
        }
        write_json(summary_path, summary)
        return {
            "ready": False,
            "result": {
                "status": "technical_eval_failed",
                "task": task,
                "summary": summary,
                "executed": False,
            },
        }
    patch_selection = bind_eval_image(row, patch_selection)
    candidate_expectation = patch_selection.get("candidate_expectation")
    if patch_selection.get("ok") and not _candidate_expectation_valid(
        candidate_expectation,
        row,
        prediction,
        patch_selection,
    ):
        patch_selection.update(ok=False, status="candidate_identity_mismatch")
    if not patch_selection.get("ok"):
        selection_status = str(patch_selection.get("status") or "gitlink_probe_failed")
        if selection_status == "blocked_missing_eval_image":
            return {
                "ready": False,
                "result": {
                    "status": "blocked_missing_eval_image",
                    "task": task,
                    "image_status": patch_selection.get("image_status"),
                    "executed": False,
                    "eval_patch_sha256": patch_selection.get("eval_patch_sha256"),
                },
            }
        summary = {
            "schema": "opencollab.prolite_direct_eval.v2",
            "status": "technical_eval_failed",
            "task": task,
            "resolved": False,
            "patch_sha256": row_patch_sha(prediction),
            "source_patch_sha256": patch_selection.get("source_patch_sha256"),
            "eval_patch_sha256": patch_selection.get("eval_patch_sha256"),
            "filtered_patch_paths": patch_selection.get("filtered_patch_paths", []),
            "gitlink_probe": patch_selection.get("gitlink_probe"),
            "eval_image_id": patch_selection.get("image_id") or "",
            "record_id": row_record_id(prediction),
            "eval_spec_sha256": eval_spec_sha256,
            "technical_reasons": [selection_status],
            "pairing": pairing,
        }
        write_json(summary_path, summary)
        return {
            "ready": False,
            "result": {
                "status": "technical_eval_failed",
                "task": task,
                "summary": summary,
                "executed": False,
                "eval_patch_sha256": patch_selection.get("eval_patch_sha256"),
                "eval_image_id": patch_selection.get("image_id") or "",
            },
        }
    model_patch = patch_selection["model_patch"]
    patch_evidence = {
        "source_patch_sha256": patch_selection["source_patch_sha256"],
        "eval_patch_sha256": patch_selection["eval_patch_sha256"],
        "filtered_patch_paths": patch_selection["filtered_patch_paths"],
        "gitlink_probe": patch_selection.get("gitlink_probe"),
        "eval_image_id": patch_selection.get("image_id") or "",
        "candidate_expectation": candidate_expectation,
    }
    if not model_patch.strip():
        summary = {
            "schema": "opencollab.prolite_direct_eval.v2",
            "status": "empty_eval_patch_invalid",
            "task": task,
            "resolved": False,
            "patch_sha256": row_patch_sha(prediction),
            **patch_evidence,
            "record_id": row_record_id(prediction),
            "eval_spec_sha256": eval_spec_sha256,
            "model_patch_chars": len(prediction_patch(prediction)),
            "eval_model_patch_chars": 0,
            "technical_reasons": ["empty_eval_patch_after_filter"],
            "pairing": pairing,
        }
        write_json(summary_path, summary)
        return {
            "ready": False,
            "result": {
                "status": "empty_eval_patch_invalid",
                "task": task,
                "summary": summary,
                "executed": False,
                "eval_patch_sha256": patch_selection["eval_patch_sha256"],
            },
        }
    return {
        "ready": True,
        "patch_selection": patch_selection,
        "model_patch": model_patch,
        "patch_evidence": patch_evidence,
    }


__all__ = ["validated_eval_patch", "verified_plan_patch_selection"]
