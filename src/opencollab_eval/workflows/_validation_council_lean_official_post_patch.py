"""Integrated post-patch verification for the validation council workflow."""

from __future__ import annotations

import json
from typing import Any

INTEGRATED_VERIFIER_BUDGET = 440_000
VERDICT_SERIALIZER_BUDGET = 80_000

POST_PATCH_EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "diff",
        "validated_patch_sha256",
        "contract_checks",
        "coverage_checks",
        "baseline_replay",
        "risk_checks",
        "failure_classifications",
        "remaining_defects",
        "retry_brief",
        "critic_reason",
        "verdict",
        "findings",
        "allowed_patch_paths",
        "protected_paths",
        "actual_disallowed_changed_files",
    ],
    "properties": {
        "diff": {
            "type": "object",
            "required": ["changed_files", "unexpected_files", "summary"],
            "properties": {
                "changed_files": {"type": "array", "items": {"type": "string"}},
                "unexpected_files": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"},
            },
        },
        "validated_patch_sha256": {"type": "string"},
        "contract_checks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["contract_id", "status", "command", "evidence"],
                "properties": {
                    "contract_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["pass", "fail", "not_run", "unsupported"],
                    },
                    "command": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        },
        "coverage_checks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["case_id", "status", "command", "evidence"],
                "properties": {
                    "case_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["pass", "fail", "not_run", "unsupported"],
                    },
                    "command": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        },
        "baseline_replay": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["test_id", "before_status", "after_status", "command", "evidence"],
                "properties": {
                    "test_id": {"type": "string"},
                    "before_status": {"type": "string"},
                    "after_status": {
                        "type": "string",
                        "enum": ["pass", "fail", "not_run", "invalid"],
                    },
                    "command": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        },
        "risk_checks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["risk", "contract_ids", "command", "result", "evidence"],
                "properties": {
                    "risk": {"type": "string"},
                    "contract_ids": {"type": "array", "items": {"type": "string"}},
                    "command": {"type": "string"},
                    "result": {
                        "type": "string",
                        "enum": ["pass", "fail", "not_run", "rejected"],
                    },
                    "evidence": {"type": "string"},
                },
            },
        },
        "failure_classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "command",
                    "classification",
                    "baseline_test_id",
                    "failure_signature",
                    "evidence",
                ],
                "properties": {
                    "command": {"type": "string"},
                    "classification": {
                        "type": "string",
                        "enum": ["unresolved", "expected_obsolete", "environmental"],
                    },
                    "baseline_test_id": {"type": "string"},
                    "failure_signature": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        },
        "remaining_defects": {"type": "array", "items": {"type": "string"}},
        "retry_brief": {
            "type": "object",
            "required": ["must_fix", "do_not_change"],
            "properties": {
                "must_fix": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": [
                            "priority",
                            "problem",
                            "evidence",
                            "contract_ids",
                            "failed_command",
                            "location",
                            "missing_case",
                            "distinguishing_probe",
                        ],
                        "properties": {
                            "priority": {"type": "integer"},
                            "problem": {"type": "string"},
                            "evidence": {"type": "string"},
                            "contract_ids": {"type": "array", "items": {"type": "string"}},
                            "failed_command": {"type": "string"},
                            "location": {"type": "string"},
                            "missing_case": {
                                "type": "string",
                                "description": "The missing state combination or behavioral distinction, or unknown.",
                            },
                            "distinguishing_probe": {
                                "type": "string",
                                "description": "Public command that distinguishes the patch, or unknown.",
                            },
                        },
                    },
                },
                "do_not_change": {"type": "array", "items": {"type": "string"}},
            },
        },
        "critic_reason": {"type": "string"},
        "verdict": {
            "type": "string",
            "enum": ["PASS", "FAIL", "BLOCKED", "NEEDS_CRITIC"],
        },
        "findings": {"type": "string"},
        "allowed_patch_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Actual changed source paths that may remain in the submitted patch.",
        },
        "protected_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Policy paths or patterns that must not be changed; "
                "informational, not proof of a violation."
            ),
        },
        "actual_disallowed_changed_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Only protected or unexpected paths actually present in the current diff.",
        },
    },
}

INTEGRATED_VERIFIER_PROMPT = """\
You are the read-only Integrated Verifier and sole owner of post-patch evidence.
Inspect the actual diff, replay useful pre-patch evidence, check every public
behavior contract, assess diff-derived risks, and run focused public tests.

Work in this order:
1. Inspect `git diff` and `git diff --name-only`; identify unexpected files.
2. Replay accepted baseline probes that remain valid after the patch.
3. Map each contract to an observed check or mark it not_run/unsupported.
4. Check risks introduced by the actual diff; add only focused public probes.
5. Run relevant regression tests using the observed repository environment.
6. Use the behavior coverage matrix as a prioritized checklist; record the
   cases actually checked and do not invent extra product requirements.
7. Return one concise evidence ledger and verdict.

The canonical working tree already contains the Coder's patch. Never run Git
commands that alter it, including stash, reset, checkout, clean, restore,
switch, commit, add, apply, or worktree operations. Do not recreate the
baseline in place. Use the supplied pre-patch baseline evidence and execute
post-patch checks against the current tree.

Echo the supplied deterministic patch SHA-256 in validated_patch_sha256. Record
exact commands and observed results. PASS requires a nonempty source
diff, no disallowed artifacts, at least one relevant executable command that
passed, and no unexplained observed product failure. A missing advisory coverage
case lowers confidence but is not by itself a code failure. Return FAIL
with a prioritized retry_brief for a concrete defect. Return BLOCKED only for
an external blocker. Return NEEDS_CRITIC only when evidence is genuinely
uncertain or the patch is high-risk (for example cross-module/API change or
weak contract coverage), never merely to avoid verification.

Each retry item must name its failed command, result, contract, and smallest evidence-backed code location.
State the missing behavioral distinction using the relevant state, option, or inheritance dimension.
Supply a focused public distinguishing_probe command that fails on this patch and passes on a correct fix.
Use unknown only when evidence lacks location, missing_case, or distinguishing_probe; never invent any of them.
Do not pipe a test command through `tail`, `head`, or another command unless
pipefail is enabled and the original test runner's exit status is preserved.

Every observed failing command must appear once in failure_classifications.
Use unresolved for a real product failure. Use expected_obsolete only when the
failure is a changed assertion value that directly contradicts the requested
new public behavior; never use it for an exception or setup failure. Use
environmental only when baseline_triage contains an observed
base_environment_failure for the referenced baseline_test_id. Environment
claims without that baseline reference and matching failure_signature are unresolved.

Keep policy declarations separate from observed diff facts. Put general path
rules in protected_paths. Put a path in actual_disallowed_changed_files only
when it is visibly present in the current diff. Do not copy protected_paths
into actual_disallowed_changed_files.

Verification mode:
{mode}

Goal:
{goal}

Contracts:
{contracts}

Accepted pre-patch validation:
{pre_judge}

Baseline evidence:
{baseline_triage}

Test cartography:
{cartography}

Coder report:
{coder_report}

Deterministic patch facts:
{diff_facts}

Previous post-patch evidence:
{previous_evidence}

Independent risk critic report:
{critic_report}

{rules}"""

VERDICT_SERIALIZER_PROMPT = """\
You are the no-tools Verdict Serializer. Convert the public evidence
below into the required post-patch evidence schema. Do not invent commands,
results, paths, or observations. A runtime-authored tool ledger is evidence
only for what its snippets explicitly show. If the evidence cannot support a
PASS, return FAIL with the most concrete available retry brief; use BLOCKED only
for an explicit external environment failure. General protected path rules are
not actual violations.
Echo deterministic diff_facts.diff_sha256 in validated_patch_sha256. Preserve
coverage checks and failure classifications visible in the supplied evidence.

Each retry_brief.must_fix item must preserve only evidence-backed command, contract, location, missing_case, and probe.
Use unknown for location, missing_case, or distinguishing_probe when the supplied evidence lacks that fact.
Do not infer or invent a state combination or probe.

Goal:
{goal}

Contracts:
{contracts}

Deterministic diff facts:
{diff_facts}

Runtime-authored tool evidence:
{tool_ledger}

Coder report:
{coder_report}

{rules}"""


def _empty_post_patch_evidence(message: str) -> dict[str, Any]:
    return {
        "diff": {"changed_files": [], "unexpected_files": [], "summary": ""},
        "validated_patch_sha256": "",
        "contract_checks": [],
        "coverage_checks": [],
        "baseline_replay": [],
        "risk_checks": [],
        "failure_classifications": [],
        "remaining_defects": [message],
        "retry_brief": {
            "must_fix": [
                {
                    "priority": 1,
                    "problem": message,
                    "evidence": "No structured verifier evidence was returned.",
                    "contract_ids": [],
                    "failed_command": "",
                    "location": "",
                    "missing_case": "unknown",
                    "distinguishing_probe": "unknown",
                }
            ],
            "do_not_change": [],
        },
        "critic_reason": "",
        "verdict": "FAIL",
        "findings": message,
        "allowed_patch_paths": [],
        "protected_paths": [],
        "actual_disallowed_changed_files": [],
        # Compatibility alias for callers that consumed the former field.
        "disallowed_patch_paths": [],
    }


def _normalize_post_patch_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _empty_post_patch_evidence("Integrated verifier returned no structured evidence.")
    fallback = _empty_post_patch_evidence("Integrated verifier evidence was incomplete.")
    evidence = {**fallback, **value}
    retry_brief = evidence.get("retry_brief")
    if not isinstance(retry_brief, dict):
        return evidence
    must_fix = retry_brief.get("must_fix")
    if not isinstance(must_fix, list):
        return evidence
    evidence["retry_brief"] = {
        **retry_brief,
        "must_fix": [
            {
                **item,
                "missing_case": str(item.get("missing_case") or "unknown"),
                "distinguishing_probe": str(item.get("distinguishing_probe") or "unknown"),
            }
            for item in must_fix
            if isinstance(item, dict)
        ],
    }
    return evidence


def _append_gate_failure(evidence: dict[str, Any], message: str) -> dict[str, Any]:
    retry_brief = evidence.get("retry_brief")
    if not isinstance(retry_brief, dict):
        retry_brief = {"must_fix": [], "do_not_change": []}
    must_fix = retry_brief.get("must_fix")
    if not isinstance(must_fix, list):
        must_fix = []
    return {
        **evidence,
        "verdict": "FAIL",
        "findings": f"{message} {evidence.get('findings', '')}".strip(),
        "remaining_defects": [message, *_string_list(evidence.get("remaining_defects"))],
        "retry_brief": {
            **retry_brief,
            "must_fix": [
                {
                    "priority": 1,
                    "problem": message,
                    "evidence": message,
                    "contract_ids": [],
                    "failed_command": "",
                    "location": "",
                    "missing_case": "unknown",
                    "distinguishing_probe": "unknown",
                },
                *must_fix,
            ],
        },
    }


def _apply_risk_critic(
    evidence: dict[str, Any],
    critic_report: dict[str, Any],
) -> dict[str, Any]:
    """Apply a concise critic review without confusing review failure with code failure."""
    recommendation = critic_report.get("recommendation")
    if recommendation == "PASS":
        return {**evidence, "verdict": "PASS"}
    if recommendation == "INCONCLUSIVE":
        message = str(critic_report.get("summary") or "Risk review was inconclusive.")
        return {
            **evidence,
            "verdict": "NEEDS_CRITIC",
            "critic_reason": message,
            "findings": message,
        }

    risks = [item for item in critic_report.get("risks", []) if isinstance(item, dict)]
    if not risks:
        return {**evidence, "verdict": "NEEDS_CRITIC"}
    retry_items = [
        {
            "priority": index,
            "problem": str(item.get("risk") or "Counterexample review found an uncovered behavior."),
            "evidence": str(item.get("evidence") or critic_report.get("summary") or "Critic finding."),
            "contract_ids": _string_list(item.get("contract_ids")),
            "failed_command": "",
            "location": str(item.get("source_path_or_unknown") or "unknown"),
            "missing_case": str(item.get("risk") or "unknown"),
            "distinguishing_probe": str(item.get("distinguishing_probe") or "unknown"),
            "origin": "critic",
        }
        for index, item in enumerate(risks, start=1)
    ]
    message = str(critic_report.get("summary") or "Risk critic found a counterexample.")
    retry_brief = evidence.get("retry_brief") if isinstance(evidence.get("retry_brief"), dict) else {}
    return {
        **evidence,
        "verdict": "FAIL",
        "findings": message,
        "remaining_defects": [str(item.get("risk") or "Uncovered behavior") for item in risks],
        "retry_brief": {
            **retry_brief,
            "must_fix": retry_items,
            "do_not_change": _string_list(retry_brief.get("do_not_change")),
        },
    }


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _has_passing_executable_evidence(evidence: dict[str, Any]) -> bool:
    checks = (
        ("contract_checks", "status"),
        ("coverage_checks", "status"),
        ("baseline_replay", "after_status"),
        ("risk_checks", "result"),
    )
    for field, status_field in checks:
        for item in evidence.get(field, []):
            if isinstance(item, dict) and item.get(status_field) == "pass" and str(item.get("command") or "").strip():
                return True
    return False


def _observed_failures(evidence: dict[str, Any]) -> list[dict[str, str]]:
    checks = (
        ("contract_checks", "status"),
        ("baseline_replay", "after_status"),
        ("risk_checks", "result"),
        ("coverage_checks", "status"),
    )
    return [
        {
            "command": str(item.get("command") or "").strip(),
            "field": field,
            "evidence": str(item.get("evidence") or "").strip(),
        }
        for field, status_field in checks
        for item in evidence.get(field, [])
        if isinstance(item, dict) and item.get(status_field) == "fail"
    ]


def _baseline_environment_failures(
    baseline_triage: dict[str, Any] | None,
) -> dict[str, str]:
    if not isinstance(baseline_triage, dict):
        return {}
    return {
        str(item.get("test_id")): str(item.get("failure_signature") or "").strip()
        for item in baseline_triage.get("classifications", [])
        if isinstance(item, dict)
        and item.get("status") == "base_environment_failure"
        and item.get("observed") is True
        and str(item.get("command") or "").strip()
        and str(item.get("failure_signature") or "").strip()
    }


def _failure_classifications(evidence: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("command") or "").strip(): item
        for item in evidence.get("failure_classifications", [])
        if isinstance(item, dict) and str(item.get("command") or "").strip()
    }


def _request_critic(evidence: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        **evidence,
        "verdict": "NEEDS_CRITIC",
        "critic_reason": f"{message} {evidence.get('critic_reason', '')}".strip(),
        "findings": f"{message} {evidence.get('findings', '')}".strip(),
    }


def _enforce_post_patch_gates(
    evidence: dict[str, Any],
    *,
    source_changed: bool | None,
    critic_may_be_requested: bool,
    actual_changed_files: list[str] | None = None,
    actual_disallowed_changed_files: list[str] | None = None,
    protected_paths: list[str] | None = None,
    patch_fingerprint: str | None = None,
    baseline_triage: dict[str, Any] | None = None,
    critic_confirmed: bool = False,
) -> dict[str, Any]:
    diff = evidence.get("diff") if isinstance(evidence.get("diff"), dict) else {}
    changed = _string_list(diff.get("changed_files"))
    if actual_changed_files is not None:
        changed = list(dict.fromkeys(actual_changed_files))
    changed_set = set(changed)
    model_actual = [
        path
        for path in (
            _string_list(diff.get("unexpected_files"))
            + _string_list(evidence.get("actual_disallowed_changed_files"))
        )
        if path in changed_set
    ]
    actual_disallowed = list(
        dict.fromkeys([*(actual_disallowed_changed_files or []), *model_actual])
    )
    evidence = {
        **evidence,
        "diff": {
            **diff,
            "changed_files": changed,
            "unexpected_files": actual_disallowed,
        },
        "allowed_patch_paths": [path for path in changed if path not in set(actual_disallowed)],
        "protected_paths": (
            list(dict.fromkeys(protected_paths))
            if protected_paths is not None
            else _string_list(evidence.get("protected_paths"))
        ),
        "actual_disallowed_changed_files": actual_disallowed,
        # Preserve the workflow result's historical name, but populate it only
        # from observed changed files rather than policy declarations.
        "disallowed_patch_paths": actual_disallowed,
    }
    diff = evidence["diff"]
    verdict = evidence.get("verdict")
    if verdict == "NEEDS_CRITIC" and not critic_may_be_requested:
        return _append_gate_failure(evidence, "Verifier remained uncertain after independent critic review.")
    if verdict != "PASS":
        return evidence
    if source_changed is False:
        return _append_gate_failure(evidence, "No tracked source change remains after validation cleanup.")
    if not _string_list(diff.get("changed_files")):
        return _append_gate_failure(evidence, "PASS evidence did not identify the changed source files.")
    if actual_disallowed:
        return _append_gate_failure(evidence, "Unexpected or disallowed files remain in the patch.")
    if patch_fingerprint is not None:
        validated_fingerprint = str(evidence.get("validated_patch_sha256") or "").strip()
        if validated_fingerprint != patch_fingerprint:
            return _append_gate_failure(
                evidence,
                "PASS evidence was not tied to the current patch fingerprint.",
            )
    failures = _observed_failures(evidence)
    classifications = _failure_classifications(evidence)
    baseline_environment_failures = _baseline_environment_failures(baseline_triage)
    has_expected_obsolete = False
    has_environmental = False
    for failure in failures:
        classification = classifications.get(failure["command"])
        kind = str(classification.get("classification") or "") if classification else ""
        if kind == "expected_obsolete" and str(classification.get("evidence") or "").strip():
            has_expected_obsolete = True
            continue
        if kind == "environmental":
            baseline_id = str(classification.get("baseline_test_id") or "") if classification else ""
            failure_signature = (
                str(classification.get("failure_signature") or "").strip()
                if classification
                else ""
            )
            if baseline_environment_failures.get(baseline_id) == failure_signature and failure_signature:
                has_environmental = True
                continue
            return _append_gate_failure(
                evidence,
                "Environmental failure lacked a matching observed baseline failure.",
            )
        return _append_gate_failure(
            evidence,
            "PASS contradicted an unresolved failing verification command.",
        )
    if has_environmental:
        message = "Verification was blocked by an environment failure reproduced on the baseline."
        return {
            **evidence,
            "verdict": "BLOCKED",
            "findings": f"{message} {evidence.get('findings', '')}".strip(),
            "remaining_defects": [message, *_string_list(evidence.get("remaining_defects"))],
        }
    if has_expected_obsolete and not critic_confirmed:
        return _request_critic(
            evidence,
            "An observed failure was classified as an obsolete public assertion and requires independent review.",
        )
    if _string_list(evidence.get("remaining_defects")):
        return _append_gate_failure(evidence, "PASS contradicted unresolved defects in the evidence ledger.")
    if not _has_passing_executable_evidence(evidence):
        return _append_gate_failure(evidence, "PASS lacked a relevant successful executable command.")
    return evidence


def _has_actionable_retry(evidence: dict[str, Any]) -> bool:
    """Whether a retry is grounded in an observed failure or a precise critic finding."""
    retry_brief = evidence.get("retry_brief")
    must_fix = retry_brief.get("must_fix") if isinstance(retry_brief, dict) else None
    if not isinstance(must_fix, list):
        return False
    unresolved_commands = {
        str(item.get("command") or "").strip()
        for item in evidence.get("failure_classifications", [])
        if isinstance(item, dict) and item.get("classification") == "unresolved"
    }
    for item in must_fix:
        if not isinstance(item, dict):
            continue
        evidence_text = str(item.get("evidence") or "").strip()
        failed_command = str(item.get("failed_command") or "").strip()
        location = str(item.get("location") or "").strip()
        probe = str(item.get("distinguishing_probe") or "").strip()
        known_location = bool(location and location != "unknown")
        if evidence_text and known_location and failed_command in unresolved_commands:
            return True
        if (
            item.get("origin") == "critic"
            and evidence_text
            and known_location
            and probe
            and probe != "unknown"
        ):
            return True
    return False


def _evidence_text(value: Any, limit: int = 24_000) -> str:
    """Serialize evidence with a middle marker while preserving both ends."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    marker = b"...[shortened]..."
    retained = max(0, limit - len(marker))
    head = raw[: retained * 2 // 3].decode("utf-8", errors="ignore")
    tail = raw[-(retained // 3) :].decode("utf-8", errors="ignore")
    return head + marker.decode() + tail


def _critic_evidence_brief(evidence: dict[str, Any]) -> str:
    return _evidence_text(evidence, 12_000)


def _retry_feedback(evidence: dict[str, Any]) -> str:
    retry_brief = evidence.get("retry_brief")
    if not isinstance(retry_brief, dict) or not retry_brief.get("must_fix"):
        retry_brief = {
            "must_fix": [
                {
                    "priority": 1,
                    "problem": str(evidence.get("findings") or "Verification failed."),
                    "evidence": str(evidence.get("findings") or "No executable evidence."),
                    "contract_ids": [],
                    "failed_command": "",
                    "location": "",
                    "missing_case": "unknown",
                    "distinguishing_probe": "unknown",
                }
            ],
            "do_not_change": [],
        }
    return _evidence_text(retry_brief, 6_000)


def _post_patch_check_count(evidence: dict[str, Any]) -> int:
    return sum(
        len(evidence.get(field, [])) if isinstance(evidence.get(field), list) else 0
        for field in ("contract_checks", "coverage_checks", "baseline_replay", "risk_checks")
    )
