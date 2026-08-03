"""validation-council-solve - contract-led validation council for SWE tasks.

This workflow turns a SWE-style issue into a compact evidence package with
localization, behavior contracts, repository test cartography, approved public
probes, baseline triage, and one authoritative coding role. The first nonempty
source candidate is frozen for external official evaluation.

It is designed for blind SWE-bench use. Roles may inspect only the issue text,
repository code, public tests, and public documentation. They must not rely on
official hidden tests, injected grader patches, or FAIL_TO_PASS node ids.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

from ._public_api import toolset

MAX_APPROVED_PRE_TESTS = 5
MAX_CODER_ROUNDS = 3
LOCALIZER_BUDGET = 220_000
EVIDENCE_BUDGET = 180_000
VALIDATION_FACTORY_BUDGET = 160_000
JUDGE_BUDGET = 100_000
TRIAGE_BUDGET = 180_000
STRUCTURED_ROLE_TIMEOUT_SECONDS = 900
CODER_ROLE_TIMEOUT_SECONDS = 1800


def _llm_aware_role_timeout(default: float) -> float:
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


def structured_role_timeout_seconds() -> float:
    """Let provider-managed retries finish before the workflow ends a role."""
    return _llm_aware_role_timeout(STRUCTURED_ROLE_TIMEOUT_SECONDS)


def coder_role_timeout_seconds() -> float:
    """Keep the coding role alive through its model client's retry window."""
    return _llm_aware_role_timeout(CODER_ROLE_TIMEOUT_SECONDS)


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
implementation-derived or hidden-grader claims.

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
Coder. Make the smallest source fix in localized files. Call one tool per turn.
Every file_read needs offset and limit at most 10. Search each named symbol once.
Read its definition, needed imports or types, continuing adjacent windows only
until that definition ends. Never scan unrelated code or repeat a search.
After ten reads or searches, edit using gathered evidence. Use an evidenced
path. Use file_write for one unique replacement; otherwise use apply_patch with
raw ---/+++/@@ text, never a Begin Patch wrapper. Inspect the resulting diff and
run focused public tests when available. Finish with the best source candidate.

Goal:
{goal}

Localization:
{localization}

Contracts:
{contracts}

Test cartography:
{cartography}

Accepted public validation:
{pre_judge}

Baseline evidence:
{baseline_triage}
{feedback_block}"""

FEEDBACK_BLOCK = """
Previous attempt feedback:
{feedback}"""

def _read_tools() -> list[Any]:
    return toolset("file_read", "grep")


def _coder_tools() -> list[Any]:
    return toolset("bash", "file_read", "file_write", "apply_patch", "run_tests", "grep", "git_diff")


def _tester_tools() -> list[Any]:
    # Baseline triage runs public probes without changing the candidate.
    return toolset("file_read", "run_tests", "grep", "git_diff")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _clip(value: Any, limit: int = EVIDENCE_TEXT_BYTES) -> str:
    text = str(value or "").strip()
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    marker = "...[shortened]..."
    retained = limit - len(marker.encode())
    head = raw[: retained * 2 // 3].decode("utf-8", errors="ignore")
    tail = raw[-(retained // 3) :].decode("utf-8", errors="ignore")
    return head + marker + tail


def _items(value: Any, limit: int = EVIDENCE_LIST_ITEMS) -> list[Any]:
    return value[:limit] if isinstance(value, list) else []


def _bounded_dump(value: Any, limit: int) -> str:
    return _clip(_dump(value), limit)


def _complete_goal(goal: str) -> str:
    """Keep every public task field visible to every solver role."""
    return goal.strip()


def _localization_brief(value: dict[str, Any], limit: int = 400) -> str:
    return _clip(
        json.dumps(
            {
                "files": [_clip(item, 100) for item in _items(value.get("files"))],
                "root_cause": _clip(value.get("root_cause_hypothesis"), 100),
                "public_api": [_clip(item, 80) for item in _items(value.get("public_api"))],
                "done": _clip(value.get("definition_of_done"), 80),
                "summary": _clip(value.get("summary"), 80),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        limit,
    )


def _contracts_brief(value: dict[str, Any], limit: int = 500) -> str:
    contracts = []
    for item in _items(value.get("contracts")):
        if isinstance(item, dict):
            contracts.append(
                {
                    "id": _clip(item.get("id"), 80),
                    "statement": _clip(item.get("statement"), 140),
                    "scope": _clip(item.get("scope"), 100),
                    "kind": _clip(item.get("behavior_kind"), 80),
                    "testability": _clip(item.get("testability"), 100),
                }
            )
    return _bounded_dump({"contracts": contracts}, limit)


def _cartography_brief(value: dict[str, Any]) -> str:
    return _bounded_dump(
        {
            "framework": _clip(value.get("framework"), 120),
            "commands": [_clip(item, 120) for item in _items(value.get("runner_commands"), 2)],
            "test_files": [_clip(item, 100) for item in _items(value.get("test_files"))],
            "guidance": _clip(value.get("temporary_test_guidance"), 120),
        },
        350,
    )


def _candidates_brief(value: dict[str, Any], cap: int, limit: int = 600) -> str:
    tests = []
    for item in _items(value.get("tests"), cap):
        if isinstance(item, dict):
            tests.append(
                {
                    "id": _clip(item.get("id"), 80),
                    "contracts": [_clip(ref, 80) for ref in _items(item.get("contract_ids"), 3)],
                    "type": _clip(item.get("type"), 80),
                    "setup": _clip(item.get("setup"), 120),
                    "assertion": _clip(item.get("assertion"), 120),
                    "base": _clip(item.get("expected_on_base"), 40),
                    "patch": _clip(item.get("expected_on_patch"), 40),
                    "command": _clip(item.get("runner_command"), 140),
                }
            )
    return _bounded_dump({"tests": tests, "abstained": bool(value.get("abstained"))}, limit)


def _judge_brief(value: dict[str, Any], limit: int = 350) -> str:
    accepted = []
    for item in _items(value.get("accepted")):
        if isinstance(item, dict):
            accepted.append(
                {
                    "id": _clip(item.get("id"), 80),
                    "priority": item.get("priority"),
                    "reason": _clip(item.get("reason"), 120),
                }
            )
    return _bounded_dump(
        {"accepted": accepted, "brief": _clip(value.get("validation_brief"), 160)},
        limit,
    )


def _triage_brief(value: dict[str, Any], limit: int = 350) -> str:
    classifications = []
    for item in _items(value.get("classifications")):
        if isinstance(item, dict):
            classifications.append(
                {
                    "id": _clip(item.get("test_id"), 80),
                    "status": _clip(item.get("status"), 80),
                    "evidence": _clip(item.get("evidence"), 140),
                }
            )
    return _bounded_dump(
        {"classifications": classifications, "brief": _clip(value.get("approved_brief"), 160)},
        limit,
    )


def _report_brief(value: Any, limit: int = REPORT_BRIEF_BYTES) -> str:
    return _clip(value, limit)


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


async def _source_diff_present(ctx: Any, exclude_paths: list[str]) -> bool | None:
    source_changed = getattr(ctx, "source_changed", None)
    if source_changed is None:
        return None
    return await source_changed(exclude_paths)
