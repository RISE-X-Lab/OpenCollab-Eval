from __future__ import annotations

import dataclasses
import runpy
from pathlib import Path

import pytest

import opencollab_eval.engine.workspace_integrity as workspace_integrity
from opencollab_eval.engine.workspace_integrity import (
    FailureScope,
    FindingOrigin,
    IntegrityAction,
    IntegrityPhase,
    WorkspaceChange,
    WorkspaceFinding,
    classify_finding,
)

STATE_KINDS = (
    "tracked_file",
    "untracked_file",
    "ignored_file",
    "temporary_file",
    "build_artifact",
    "cache_directory",
    "broken_symlink",
    "outward_symlink",
    "hardlink",
    "gitlink",
    "submodule",
    "nested_repository",
    "git_worktree",
    "remote_ref",
    "replace_ref",
    "future_object",
    "file_mode",
    "special_file",
    "lock_file",
    "background_write",
    "other_task_artifact",
    "test_artifact",
)

CLEANABLE = {
    "untracked_file",
    "ignored_file",
    "temporary_file",
    "build_artifact",
    "cache_directory",
    "nested_repository",
    "git_worktree",
    "remote_ref",
    "replace_ref",
    "future_object",
    "lock_file",
    "other_task_artifact",
    "test_artifact",
}

UNREPRESENTABLE = {"outward_symlink", "hardlink", "special_file", "background_write"}


def test_injected_policy_imports_with_legacy_dataclass(monkeypatch: pytest.MonkeyPatch) -> None:
    original = dataclasses.dataclass

    def legacy_dataclass(*args, **kwargs):
        if "slots" in kwargs:
            raise TypeError("dataclass() got an unexpected keyword argument 'slots'")
        return original(*args, **kwargs)

    monkeypatch.setattr(dataclasses, "dataclass", legacy_dataclass)
    namespace = runpy.run_path(str(Path(workspace_integrity.__file__)))

    finding = namespace["WorkspaceFinding"](
        kind="legacy_image",
        phase=namespace["IntegrityPhase"].BASELINE,
        origin=namespace["FindingOrigin"].BASE_COMMIT,
    )
    assert namespace["classify_finding"](finding).action.value == "allow"


@pytest.mark.parametrize("kind", STATE_KINDS)
@pytest.mark.parametrize("scenario", ("baseline", "model_added", "model_modified", "unknown"))
def test_integrity_state_matrix(kind: str, scenario: str) -> None:
    if scenario == "baseline":
        finding = WorkspaceFinding(
            kind=kind,
            phase=IntegrityPhase.BASELINE,
            origin=FindingOrigin.BASE_COMMIT,
            solver_readable=True,
        )
        expected = IntegrityAction.ALLOW
    elif scenario in {"model_added", "model_modified"}:
        finding = WorkspaceFinding(
            kind=kind,
            phase=IntegrityPhase.POST_SOLVER,
            origin=FindingOrigin.CURRENT_RUN,
            change=(
                WorkspaceChange.ADDED
                if scenario == "model_added"
                else WorkspaceChange.MODIFIED
            ),
            solver_readable=True,
            candidate_effect=True,
            representable_in_patch=kind not in UNREPRESENTABLE,
        )
        expected = (
            IntegrityAction.TASK_FAILURE
            if kind in UNREPRESENTABLE
            else IntegrityAction.ALLOW
        )
    else:
        finding = WorkspaceFinding(
            kind=kind,
            phase=IntegrityPhase.BASELINE,
            origin=FindingOrigin.UNKNOWN,
            solver_readable=True,
            repairable=kind in CLEANABLE,
            removal_changes_semantics=kind not in CLEANABLE,
        )
        expected = (
            IntegrityAction.SANITIZE
            if kind in CLEANABLE
            else IntegrityAction.TASK_FAILURE
        )

    decision = classify_finding(finding)

    assert decision.action is expected
    assert decision.scope is (
        FailureScope.IMAGE
        if scenario == "unknown" and expected is IntegrityAction.TASK_FAILURE
        else FailureScope.TASK
        if scenario.startswith("model") and expected is IntegrityAction.TASK_FAILURE
        else FailureScope.NONE
    )


def test_shared_probe_is_the_only_batch_pause_path() -> None:
    failed = classify_finding(
        WorkspaceFinding(
            kind="docker_daemon",
            phase=IntegrityPhase.SHARED_PROBE,
            origin=FindingOrigin.RUNTIME_DEPENDENCY,
            shared_probe_failed=True,
        )
    )
    passed = classify_finding(
        WorkspaceFinding(
            kind="shared_storage",
            phase=IntegrityPhase.SHARED_PROBE,
            origin=FindingOrigin.RUNTIME_DEPENDENCY,
        )
    )

    assert failed.action is IntegrityAction.PAUSE_BATCH
    assert failed.scope is FailureScope.SHARED_INFRASTRUCTURE
    assert passed.action is IntegrityAction.ALLOW
    assert passed.scope is FailureScope.NONE


def test_unknown_post_solver_state_remains_local() -> None:
    decision = classify_finding(
        WorkspaceFinding(
            kind="other_task_artifact",
            phase=IntegrityPhase.POST_SOLVER,
            origin=FindingOrigin.UNKNOWN,
            solver_readable=True,
            evidence_effect=True,
        )
    )
    assert decision.action is IntegrityAction.TASK_FAILURE
    assert decision.scope is FailureScope.TASK


@pytest.mark.parametrize("phase", (IntegrityPhase.BASELINE, IntegrityPhase.POST_SOLVER))
def test_untrusted_inert_state_is_allowed_when_solver_cannot_read_it(
    phase: IntegrityPhase,
) -> None:
    decision = classify_finding(
        WorkspaceFinding(
            kind="unmaterialized_component",
            phase=phase,
            origin=FindingOrigin.UNKNOWN,
            solver_readable=False,
            visibility_and_effects_proven=True,
        )
    )

    assert decision.action is IntegrityAction.ALLOW
    assert decision.scope is FailureScope.NONE


@pytest.mark.parametrize(
    "effect",
    ("candidate_effect", "test_effect", "evidence_effect", "identity_effect"),
)
@pytest.mark.parametrize("phase", (IntegrityPhase.BASELINE, IntegrityPhase.POST_SOLVER))
def test_unreadable_unknown_state_still_fails_when_it_affects_evaluation(
    effect: str,
    phase: IntegrityPhase,
) -> None:
    decision = classify_finding(
        WorkspaceFinding(
            kind="opaque_image_state",
            phase=phase,
            origin=FindingOrigin.UNKNOWN,
            solver_readable=False,
            visibility_and_effects_proven=True,
            **{effect: True},
        )
    )

    assert decision.action is IntegrityAction.TASK_FAILURE
    assert decision.scope is (
        FailureScope.IMAGE
        if phase is IntegrityPhase.BASELINE
        else FailureScope.TASK
    )


@pytest.mark.parametrize("phase", (IntegrityPhase.BASELINE, IntegrityPhase.POST_SOLVER))
def test_omitted_visibility_proof_fails_closed(phase: IntegrityPhase) -> None:
    decision = classify_finding(
        WorkspaceFinding(
            kind="unknown_sidecar",
            phase=phase,
            origin=FindingOrigin.UNKNOWN,
        )
    )

    assert decision.action is IntegrityAction.TASK_FAILURE
    assert decision.scope is (
        FailureScope.IMAGE
        if phase is IntegrityPhase.BASELINE
        else FailureScope.TASK
    )


def test_trusted_baseline_state_is_kept_when_removal_would_change_semantics() -> None:
    decision = classify_finding(
        WorkspaceFinding(
            kind="tracked_symlink",
            phase=IntegrityPhase.BASELINE,
            origin=FindingOrigin.BASE_COMMIT,
            solver_readable=True,
            removal_changes_semantics=True,
        )
    )

    assert decision.action is IntegrityAction.ALLOW
    assert decision.scope is FailureScope.NONE


def test_verified_public_preparation_becomes_part_of_the_solver_baseline() -> None:
    decision = classify_finding(
        WorkspaceFinding(
            kind="tracked_content_drift",
            phase=IntegrityPhase.BASELINE,
            origin=FindingOrigin.PUBLIC_INPUT,
            change=WorkspaceChange.MODIFIED,
            solver_readable=True,
            candidate_effect=True,
            repairable=True,
        )
    )

    assert decision.action is IntegrityAction.ALLOW
    assert decision.scope is FailureScope.NONE
