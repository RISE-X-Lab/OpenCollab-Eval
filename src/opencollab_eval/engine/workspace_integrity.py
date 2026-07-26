"""Small, shared policy model for workspace and runtime integrity findings."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum


class IntegrityPhase(str, Enum):
    BASELINE = "baseline"
    POST_SOLVER = "post_solver"
    SHARED_PROBE = "shared_probe"


class FindingOrigin(str, Enum):
    BASE_COMMIT = "base_commit"
    PUBLIC_INPUT = "public_input"
    RUNTIME_DEPENDENCY = "runtime_dependency"
    CURRENT_RUN = "current_run"
    OTHER_RUN = "other_run"
    UNKNOWN = "unknown"


class WorkspaceChange(str, Enum):
    UNCHANGED = "unchanged"
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


class IntegrityAction(str, Enum):
    ALLOW = "allow"
    SANITIZE = "sanitize_then_continue"
    TASK_FAILURE = "task_technical_failure"
    PAUSE_BATCH = "pause_batch"


class FailureScope(str, Enum):
    NONE = "none"
    TASK = "task"
    IMAGE = "image"
    SHARED_INFRASTRUCTURE = "shared_infrastructure"


class WorkspaceIntegrityError(RuntimeError):
    """A structured workspace failure that remains local unless directly probed."""

    def __init__(
        self,
        message: str,
        *,
        scope: FailureScope,
        report: dict[str, object],
    ) -> None:
        super().__init__(message)
        self.failure_scope = scope
        self.integrity_report = report


@dataclass(frozen=True)
class WorkspaceFinding:
    """Facts collected by an execution layer without policy conclusions."""

    kind: str
    phase: IntegrityPhase
    origin: FindingOrigin
    change: WorkspaceChange = WorkspaceChange.UNCHANGED
    solver_readable: bool = False
    candidate_effect: bool = False
    test_effect: bool = False
    evidence_effect: bool = False
    identity_effect: bool = False
    repairable: bool = False
    removal_changes_semantics: bool = False
    representable_in_patch: bool = True
    shared_probe_failed: bool = False
    visibility_and_effects_proven: bool = False
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class IntegrityDecision:
    action: IntegrityAction
    scope: FailureScope
    basis: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


_TRUSTED_ORIGINS = {
    FindingOrigin.BASE_COMMIT,
    FindingOrigin.PUBLIC_INPUT,
    FindingOrigin.RUNTIME_DEPENDENCY,
}


def classify_finding(finding: WorkspaceFinding) -> IntegrityDecision:
    """Classify one finding by provenance, visibility, timing, and impact."""
    if finding.phase is IntegrityPhase.SHARED_PROBE:
        if finding.shared_probe_failed:
            return IntegrityDecision(
                IntegrityAction.PAUSE_BATCH,
                FailureScope.SHARED_INFRASTRUCTURE,
                "a direct shared-service probe failed",
            )
        return IntegrityDecision(
            IntegrityAction.ALLOW,
            FailureScope.NONE,
            "the shared-service probe passed",
        )

    affects_result = any(
        (
            finding.candidate_effect,
            finding.test_effect,
            finding.evidence_effect,
            finding.identity_effect,
        )
    )
    if finding.phase is IntegrityPhase.BASELINE:
        if (
            finding.origin in _TRUSTED_ORIGINS
            and (
                finding.change is WorkspaceChange.UNCHANGED
                or finding.origin in {FindingOrigin.PUBLIC_INPUT, FindingOrigin.RUNTIME_DEPENDENCY}
            )
        ):
            return IntegrityDecision(
                IntegrityAction.ALLOW,
                FailureScope.NONE,
                "the baseline state has a verified task input or runtime source",
            )
        if finding.repairable and not finding.removal_changes_semantics:
            return IntegrityDecision(
                IntegrityAction.SANITIZE,
                FailureScope.NONE,
                "the state can be removed from the disposable task copy without changing task semantics",
            )
        if (
            finding.visibility_and_effects_proven
            and not finding.solver_readable
            and not affects_result
        ):
            return IntegrityDecision(
                IntegrityAction.ALLOW,
                FailureScope.NONE,
                "the untrusted baseline state is unreadable to the solver and cannot affect evaluation",
            )
        return IntegrityDecision(
            IntegrityAction.TASK_FAILURE,
            FailureScope.IMAGE,
            "the task image contains solver-visible or result-affecting state with no safe provenance",
        )

    if finding.origin is FindingOrigin.CURRENT_RUN:
        if affects_result and not finding.representable_in_patch:
            return IntegrityDecision(
                IntegrityAction.TASK_FAILURE,
                FailureScope.TASK,
                "the current run changed evaluation state that the candidate patch cannot represent",
            )
        if finding.repairable and not finding.removal_changes_semantics and not affects_result:
            return IntegrityDecision(
                IntegrityAction.SANITIZE,
                FailureScope.NONE,
                "the current run created a disposable artifact with no evaluation effect",
            )
        return IntegrityDecision(
            IntegrityAction.ALLOW,
            FailureScope.NONE,
            "the current run produced a candidate-visible change that can be represented and verified",
        )

    if finding.origin in _TRUSTED_ORIGINS and finding.change is WorkspaceChange.UNCHANGED:
        return IntegrityDecision(
            IntegrityAction.ALLOW,
            FailureScope.NONE,
            "trusted baseline state remained unchanged",
        )
    if finding.repairable and not finding.removal_changes_semantics and not affects_result:
        return IntegrityDecision(
            IntegrityAction.SANITIZE,
            FailureScope.NONE,
            "an unrelated post-run artifact can be removed and rechecked",
        )
    if (
        finding.visibility_and_effects_proven
        and not finding.solver_readable
        and not affects_result
    ):
        return IntegrityDecision(
            IntegrityAction.ALLOW,
            FailureScope.NONE,
            "the untrusted post-run state is unreadable and has no evaluation effect",
        )
    return IntegrityDecision(
        IntegrityAction.TASK_FAILURE,
        FailureScope.TASK,
        "post-run state has unknown or cross-run provenance and may affect the result",
    )


def classify_findings(findings: list[WorkspaceFinding]) -> list[IntegrityDecision]:
    return [classify_finding(finding) for finding in findings]


def failure_report(
    finding: WorkspaceFinding,
    *,
    verified_state_after_action: str,
) -> dict[str, object]:
    """Build the common report carried by a rejected workspace operation."""
    decision = classify_finding(finding)
    return {
        "schema": "opencollab.workspace_integrity.v1",
        "findings": [
            {
                "observed_state": finding.as_dict(),
                "classification_basis": decision.basis,
                "action": decision.action.value,
                "verified_state_after_action": verified_state_after_action,
                "failure_scope": decision.scope.value,
            }
        ],
        "outcome": decision.action.value,
        "failure_scope": decision.scope.value,
    }


__all__ = [
    "FailureScope",
    "FindingOrigin",
    "IntegrityAction",
    "IntegrityDecision",
    "IntegrityPhase",
    "WorkspaceChange",
    "WorkspaceFinding",
    "WorkspaceIntegrityError",
    "classify_finding",
    "classify_findings",
    "failure_report",
]
