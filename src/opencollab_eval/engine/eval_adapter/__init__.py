"""Evaluation adapter contracts and pro-lite helpers."""

from opencollab_eval.engine.eval_adapter.models import (
    EvalResult,
    PatchCandidate,
    RunRecord,
    TaskSpec,
    WorkspaceSpec,
)
from opencollab_eval.engine.eval_adapter.prolite import (
    DEFAULT_DATASET_NAME,
    DEFAULT_REPO_ROOT_CANDIDATES,
    PROLITE_IMAGE_PREFIX,
    classify_technical_failure,
    is_technical_failure,
    load_jsonl_dataset,
    patch_candidate_from_diff,
    select_repo_root,
    task_spec_from_row,
    workspace_spec_for_task,
)

__all__ = [
    "DEFAULT_DATASET_NAME",
    "DEFAULT_REPO_ROOT_CANDIDATES",
    "PROLITE_IMAGE_PREFIX",
    "EvalResult",
    "PatchCandidate",
    "RunRecord",
    "TaskSpec",
    "WorkspaceSpec",
    "classify_technical_failure",
    "is_technical_failure",
    "load_jsonl_dataset",
    "patch_candidate_from_diff",
    "select_repo_root",
    "task_spec_from_row",
    "workspace_spec_for_task",
]
