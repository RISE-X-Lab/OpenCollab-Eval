"""validation-council-solve - contract-led validation council for SWE tasks.

This workflow turns a SWE-style issue into a sequence of auditable artifacts:
localization, behavior contracts, repository test cartography, candidate
validation probes, judge decisions, baseline triage, coding, diff risk audit,
post-patch probes, and final verification.

It is designed for blind SWE-bench use. Roles may inspect only the issue text,
repository code, public tests, and public documentation. They must not rely on
official hidden tests, injected grader patches, or FAIL_TO_PASS node ids.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

from ._public_api import role_feedback, toolset

MAX_APPROVED_PRE_TESTS = 5
MAX_APPROVED_POST_TESTS = 4
WORKFLOW_VARIANT = "G1.1"
MAX_CODER_ROUNDS = 3
LOCALIZER_BUDGET = int(os.environ.get("OPENCOLLAB_G11_ROLE_BUDGET", "220000"))
EVIDENCE_BUDGET = int(os.environ.get("OPENCOLLAB_G11_ROLE_BUDGET", "180000"))
VALIDATION_FACTORY_BUDGET = int(os.environ.get("OPENCOLLAB_G11_ROLE_BUDGET", "160000"))
JUDGE_BUDGET = int(os.environ.get("OPENCOLLAB_G11_ROLE_BUDGET", "100000"))
TRIAGE_BUDGET = int(os.environ.get("OPENCOLLAB_G11_ROLE_BUDGET", "180000"))
RISK_BUDGET = int(os.environ.get("OPENCOLLAB_G11_ROLE_BUDGET", "60000"))
VERIFIER_BUDGET = int(os.environ.get("OPENCOLLAB_G11_ROLE_BUDGET", "220000"))
STRUCTURED_ROLE_TIMEOUT_SECONDS = 900
CODER_ROLE_TIMEOUT_SECONDS = 1800


def _llm_aware_role_timeout(default: float) -> float | None:
    if os.environ.get("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION") == "1":
        return None
    raw = os.environ.get("OPENCOLLAB_LLM_TIMEOUT")
    if raw is None:
        return default
    try:
        llm_timeout = float(raw)
    except ValueError as exc:
        raise ValueError("OPENCOLLAB_LLM_TIMEOUT must be a positive finite number") from exc
    if not math.isfinite(llm_timeout) or llm_timeout <= 0:
        raise ValueError("OPENCOLLAB_LLM_TIMEOUT must be a positive finite number")
    return max(default, llm_timeout + 60)


def structured_role_timeout_seconds() -> float | None:
    """Let provider-managed retries finish before the workflow ends a role."""
    return _llm_aware_role_timeout(STRUCTURED_ROLE_TIMEOUT_SECONDS)


def coder_role_timeout_seconds() -> float | None:
    """Keep the coding role alive through its model client's retry window."""
    return _llm_aware_role_timeout(CODER_ROLE_TIMEOUT_SECONDS)


EMPTY_POST_CANDIDATES = {
    "tests": [],
    "abstained": True,
    "rationale": "Post-patch validation skipped.",
}
EMPTY_POST_JUDGE = {
    "accepted": [],
    "rejected": [],
    "diagnostic": [],
    "validation_brief": "Post-patch validation skipped.",
}
EMPTY_POST_TRIAGE = {
    "classifications": [],
    "approved_brief": "Post-patch triage skipped.",
    "abstained": True,
}
EMPTY_DIFF_RISKS = {
    "risks": [],
    "summary": "Diff risk audit skipped.",
}

SHARED_RULES = """\
Rules:
- Use public issue, repository, test, and documentation evidence only.
- Never use hidden grader data, official hidden tests, grader patches, or FAIL_TO_PASS IDs.
- Obey this role and its tools.
- Keep probes under /tmp/opencollab-validation-* and out of the patch.
- Report unavailable probes as not_run. Make the smallest source fix.
- Read-only roles do not search for write tools.
- Do not run git commit."""

EVIDENCE_TEXT_BYTES = 160
REPORT_BRIEF_BYTES = 320
EVIDENCE_LIST_ITEMS = 3

LOCALIZATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "summary",
        "root_cause_hypothesis",
        "files",
        "public_api",
        "uncertainties",
        "definition_of_done",
    ],
    "properties": {
        "summary": {"type": "string"},
        "root_cause_hypothesis": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
        "public_api": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "definition_of_done": {"type": "string"},
    },
}

CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["contracts"],
    "properties": {
        "contracts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "statement",
                    "scope",
                    "behavior_kind",
                    "evidence",
                    "confidence",
                    "testability",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "statement": {"type": "string"},
                    "scope": {"type": "string"},
                    "behavior_kind": {
                        "type": "string",
                        "enum": ["desired", "current_buggy", "existing_unaffected"],
                    },
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["source_type", "file_or_section", "summary"],
                            "properties": {
                                "source_type": {"type": "string"},
                                "file_or_section": {"type": "string"},
                                "summary": {"type": "string"},
                            },
                        },
                    },
                    "confidence": {"type": "string"},
                    "testability": {"type": "string"},
                },
            },
        },
    },
}

TEST_CARTOGRAPHY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "framework",
        "runner_commands",
        "test_files",
        "fixtures",
        "assertion_style",
        "temporary_test_guidance",
    ],
    "properties": {
        "framework": {"type": "string"},
        "runner_commands": {"type": "array", "items": {"type": "string"}},
        "test_files": {"type": "array", "items": {"type": "string"}},
        "fixtures": {"type": "array", "items": {"type": "string"}},
        "assertion_style": {"type": "string"},
        "temporary_test_guidance": {"type": "string"},
    },
}

CANDIDATE_TESTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["tests", "abstained", "rationale"],
    "properties": {
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "contract_ids",
                    "type",
                    "oracle_type",
                    "setup",
                    "assertion",
                    "expected_on_base",
                    "expected_on_patch",
                    "why_distinguishes_wrong_patch",
                    "evidence_refs",
                    "runner_command",
                    "risk_of_false_positive",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "contract_ids": {"type": "array", "items": {"type": "string"}},
                    "type": {
                        "type": "string",
                        "enum": ["repro", "edge", "regression", "metamorphic", "diagnostic"],
                    },
                    "oracle_type": {"type": "string"},
                    "setup": {"type": "string"},
                    "assertion": {"type": "string"},
                    "expected_on_base": {"type": "string", "enum": ["fail", "pass", "unknown"]},
                    "expected_on_patch": {"type": "string", "enum": ["pass", "unknown"]},
                    "why_distinguishes_wrong_patch": {"type": "string"},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                    "runner_command": {"type": "string"},
                    "risk_of_false_positive": {"type": "string"},
                },
            },
        },
        "abstained": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
}

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["accepted", "rejected", "diagnostic", "validation_brief"],
    "properties": {
        "accepted": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "priority", "classification", "reason"],
                "properties": {
                    "id": {"type": "string"},
                    "priority": {"type": "integer"},
                    "classification": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "rejected": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "reason"],
                "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
            },
        },
        "diagnostic": {"type": "array", "items": {"type": "string"}},
        "validation_brief": {"type": "string"},
    },
}

TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["classifications", "approved_brief", "abstained"],
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["test_id", "status", "evidence"],
                "properties": {
                    "test_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": [
                            "base_fail_repro",
                            "base_pass_regression",
                            "patch_pass",
                            "patch_fail",
                            "invalid",
                            "weak",
                            "not_run",
                        ],
                    },
                    "evidence": {"type": "string"},
                },
            },
        },
        "approved_brief": {"type": "string"},
        "abstained": {"type": "boolean"},
    },
}

DIFF_RISK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["risks", "summary"],
    "properties": {
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "changed_area", "risk", "contract_ids", "suggested_probe", "priority"],
                "properties": {
                    "id": {"type": "string"},
                    "changed_area": {"type": "string"},
                    "risk": {"type": "string"},
                    "contract_ids": {"type": "array", "items": {"type": "string"}},
                    "suggested_probe": {"type": "string"},
                    "priority": {"type": "integer"},
                },
            },
        },
        "summary": {"type": "string"},
    },
}

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "findings", "allowed_patch_paths", "disallowed_patch_paths"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL", "BLOCKED"]},
        "findings": {
            "type": "string",
            "description": "Commands run, evidence observed, and remaining defect or blocker.",
        },
        "allowed_patch_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Paths from git diff --name-only that are legitimate source changes.",
        },
        "disallowed_patch_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Temporary validation files, tests, logs, caches, or other non-submission paths.",
        },
    },
}

LOCALIZER_PROMPT = """\
You are the read-only Analyst. Locate the likely source, public API, root cause,
unknowns, and definition of done using public repository evidence.
Never use hidden grader data. Read-only roles do not search for write tools.
Report unavailable probes as not_run. Do not run git commit.

Goal:
{goal}"""

CONTRACT_MINER_PROMPT = """\
You are the read-only Contract Miner. Extract behavior contracts grounded in
the issue, source, public docs, or public tests. Never infer hidden assertions.

Goal:
{goal}

Localization:
{localization}"""

TEST_CARTOGRAPHER_PROMPT = """\
You are the read-only Test Cartographer. Identify the runner, relevant public
tests, fixtures, assertion style, and safe temporary probe method.

Goal:
{goal}

Localization:
{localization}"""

PRE_VALIDATION_FACTORY_PROMPT = """\
You are the read-only Pre-Patch Validation Factory. Propose short public
evidence-backed probes linked to contract ids. Do not edit or run them. Mark
weak probes and abstain when evidence is insufficient.

Goal:
{goal}

Contracts:
{contracts}

Test cartography:
{cartography}"""

JUDGE_PROMPT = """\
You are the read-only Validation Judge for {stage}. Accept at most {cap}
public-evidence probes. Reject missing contract ids, unsupported assertions,
implementation-derived or hidden-grader claims. Use each probe's existing
candidate id in accepted and rejected entries. Contract ids identify
requirements and must not replace candidate ids.

Goal:
{goal}

Contracts:
{contracts}

Candidates:
{candidates}"""

BASELINE_TRIAGE_PROMPT = """\
You are the Baseline Executor. Run accepted cheap probes and record exact
commands. Classify each as base_fail_repro, base_pass_regression, invalid, weak,
or not_run. Keep temporary files outside the patch.

Goal:
{goal}

Accepted validation:
{judge}"""

CODER_PROMPT = """\
You are the Coder. Implement the source fix using the complete evidence package.
Inspect the relevant definitions and callers, and address every public behavior
requirement. Run relevant public tests and accepted validation probes when useful.
Use the tool schemas for valid edit and command syntax. Continue this coding pass
until the source fix and its available verification are complete. Report changed
files, verification results, and any remaining concrete defects.

{rules}

Goal:
{goal}

Localization:
{localization}

Contracts:
{contracts}

Test cartography:
{cartography}

Pre-patch validation:
{pre_judge}

Baseline triage:
{baseline_triage}
{feedback_block}"""


FEEDBACK_BLOCK = """
Previous attempt feedback:
{feedback}"""

PATCH_VALIDATOR_PROMPT = """\
You are the read-only Patch Validator. Inspect the diff and run relevant public
tests. PASS requires a minimal source change and executable evidence. Run
`git diff --name-only`. Put source paths in allowed_patch_paths and tests,
caches, logs, or generated files in disallowed_patch_paths. Empty diff is FAIL.

Goal:
{goal}

Coder report:
{coder_report}

Accepted validation:
{pre_judge}

Baseline triage:
{baseline_triage}"""

DIFF_RISK_PROMPT = """\
You are the read-only Diff Risk Auditor. From the supplied contracts and
verdict, identify at most three semantic risks and focused probes. Return no
risks when the evidence is sufficient.

Goal:
{goal}

Contracts:
{contracts}

Patch validator verdict:
{patch_verdict}"""

POST_VALIDATION_FACTORY_PROMPT = """\
You are the read-only Post-Patch Validation Factory. Propose public
evidence-backed probes for remaining risks. Do not edit or run them. Abstain
when risks are empty or evidence is insufficient.

Goal:
{goal}

Contracts:
{contracts}

Diff risks:
{risks}"""

POST_TRIAGE_PROMPT = """\
You are the Post-Patch Executor. Run accepted cheap probes, keep temporary
files outside the patch, and classify each as patch_pass, patch_fail, invalid,
weak, or not_run. Record exact commands.

Goal:
{goal}

Accepted post-patch validation:
{judge}"""

FINAL_VERIFIER_PROMPT = """\
You are the read-only Final Verifier. Inspect the diff and run relevant public
tests. PASS requires a source change, clean executable evidence, and no
temporary artifact in the diff. Run `git diff --name-only`. Empty diff is FAIL.

Goal:
{goal}

Patch validator verdict:
{patch_verdict}

Post-patch triage:
{post_triage}"""


def _read_tools() -> list[Any]:
    return toolset("file_read", "grep")


def _coder_tools() -> list[Any]:
    return toolset("bash", "file_read", "file_write", "apply_patch", "run_tests", "grep")


def _tester_tools() -> list[Any]:
    # Gate roles (patch-validator, post-triage, final-verifier) need an executable
    # probe so a PASS is backed by a real run, not prose alone. Blindness holds
    # because the hidden FAIL_TO_PASS tests are absent from the container, not
    # because bash is. No file_write/apply_patch: these roles verify, not author.
    return toolset("bash", "file_read", "run_tests", "grep", "git_diff")


def _risk_tools() -> list[Any]:
    # The diff-risk auditor must at least read the diff and the sources it judges;
    # an empty toolset let it "audit" blind. Read-only — no execution or authoring.
    return toolset("file_read", "grep", "git_diff")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _clip(value: Any, limit: int = EVIDENCE_TEXT_BYTES) -> str:
    """Retain complete role text; the legacy byte limit is no longer applied."""
    del limit
    return "" if value is None else str(value)


def _items(value: Any, limit: int = EVIDENCE_LIST_ITEMS) -> list[Any]:
    del limit
    return list(value) if isinstance(value, list) else []


def _bounded_dump(value: Any, limit: int) -> str:
    del limit
    return _dump(value)


def _complete_goal(goal: str) -> str:
    """Keep every public task field visible to every solver role."""
    return goal.strip()


def _localization_brief(value: dict[str, Any], limit: int = 400) -> str:
    del limit
    return _dump(value)


def _contracts_brief(value: dict[str, Any], limit: int = 500) -> str:
    del limit
    return _dump(value)


def _cartography_brief(value: dict[str, Any]) -> str:
    return _dump(value)


def _candidates_brief(value: dict[str, Any], cap: int, limit: int = 600) -> str:
    # The judge's approval cap is applied by _trim_judge, after seeing all proposals.
    del cap, limit
    return _dump(value)


def _judge_brief(value: dict[str, Any], limit: int = 350) -> str:
    del limit
    return _dump(value)


def _triage_brief(value: dict[str, Any], limit: int = 350) -> str:
    del limit
    return _dump(value)


def _risks_brief(value: dict[str, Any], limit: int = 350) -> str:
    del limit
    return _dump(value)


def _verdict_brief(value: dict[str, Any]) -> str:
    return _dump(value)


def _report_brief(value: Any, limit: int = REPORT_BRIEF_BYTES) -> str:
    del limit
    return _dump(value) if isinstance(value, (dict, list)) else str(value or "")


def _dict_or(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    return value if isinstance(value, dict) else fallback


def _trim_judge(judge: dict[str, Any], cap: int) -> dict[str, Any]:
    accepted = judge.get("accepted")
    if isinstance(accepted, list):
        judge = {**judge, "accepted": accepted[:cap]}
    return judge


def _accepted_count(judge: Any) -> int:
    if isinstance(judge, dict) and isinstance(judge.get("accepted"), list):
        return len(judge["accepted"])
    return 0


def _is_pass(verdict: Any) -> bool:
    return isinstance(verdict, dict) and verdict.get("verdict") == "PASS"


def _is_blocked(verdict: Any) -> bool:
    return isinstance(verdict, dict) and verdict.get("verdict") == "BLOCKED"


async def _source_diff_present(ctx: Any, exclude_paths: list[str]) -> bool | None:
    source_changed = getattr(ctx, "source_changed", None)
    if source_changed is None:
        return None
    return await source_changed(exclude_paths)


def _feedback(*reports: Any) -> str:
    return role_feedback(*reports)
