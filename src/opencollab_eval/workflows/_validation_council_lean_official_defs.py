"""validation-council-solve - contract-led validation council for SWE tasks.

This workflow turns a SWE-style issue into a sequence of auditable artifacts:
localization, behavior contracts, repository test cartography, candidate
validation probes, judge decisions, baseline triage, coding, and integrated
post-patch verification with a concise critic for genuine uncertainty.

It is designed for blind SWE-bench use. Roles may inspect only the issue text,
repository code, public tests, and public documentation. They must not rely on
official hidden tests, injected grader patches, or FAIL_TO_PASS node ids.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

from ._validation_council_lean_official_tools import toolset, verifier_toolset

MAX_APPROVED_PRE_TESTS = 5
WORKFLOW_VARIANT = "G1.1"
MAX_CODER_ROUNDS = 3
LOCALIZER_BUDGET = 220_000
EVIDENCE_BUDGET = 180_000
VALIDATION_FACTORY_BUDGET = 160_000
JUDGE_BUDGET = 100_000
TRIAGE_BUDGET = 180_000
EVIDENCE_TEXT_BYTES = 160
REPORT_BRIEF_BYTES = 320
EVIDENCE_LIST_ITEMS = 3
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
- Use only public issue, code, tests and docs.
- Never inspect hidden tests, benchmark harnesses, grader patches or FAIL_TO_PASS IDs.
- Repository files stay in `{workspace_root}`. Use `grep` for searches; do not search with Bash or Python.
- A probe is a validation plan; report unavailable probes as not_run.
- Keep probes in /tmp/opencollab-validation-*, outside the patch.
- Obey role tools; read-only roles do not search for write tools.
- Make the smallest fix. Do not run git commit."""

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
    "required": ["contracts", "coverage_matrix"],
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
        "coverage_matrix": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
                "type": "object",
                "required": [
                    "case_id",
                    "contract_ids",
                    "state_dimensions",
                    "expected_behavior",
                    "relevant_source_path",
                    "suggested_probe",
                    "priority",
                ],
                "properties": {
                    "case_id": {"type": "string"},
                    "contract_ids": {"type": "array", "items": {"type": "string"}},
                    "state_dimensions": {"type": "string"},
                    "expected_behavior": {"type": "string"},
                    "relevant_source_path": {"type": "string"},
                    "suggested_probe": {"type": "string"},
                    "priority": {"type": "integer"},
                },
            },
        },
    },
}
TEST_CARTOGRAPHY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "framework",
        "working_runner_commands",
        "runner_commands",
        "test_files",
        "fixtures",
        "assertion_style",
        "available_tools",
        "unavailable_tools",
        "available_dependencies",
        "unavailable_dependencies",
        "capabilities",
        "diagnostics",
        "temporary_test_guidance",
    ],
    "properties": {
        "framework": {"type": "string"},
        "working_runner_commands": {"type": "array", "items": {"type": "string"}},
        "runner_commands": {"type": "array", "items": {"type": "string"}},
        "test_files": {"type": "array", "items": {"type": "string"}},
        "fixtures": {"type": "array", "items": {"type": "string"}},
        "assertion_style": {"type": "string"},
        "available_tools": {"type": "array", "items": {"type": "string"}},
        "unavailable_tools": {"type": "array", "items": {"type": "string"}},
        "available_dependencies": {"type": "array", "items": {"type": "string"}},
        "unavailable_dependencies": {"type": "array", "items": {"type": "string"}},
        "capabilities": {"type": "array", "items": {"type": "string"}},
        "diagnostics": {"type": "array", "items": {"type": "string"}},
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
                "required": [
                    "test_id",
                    "status",
                    "command",
                    "observed",
                    "evidence",
                    "failure_signature",
                ],
                "properties": {
                    "test_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": [
                            "base_fail_repro",
                            "base_pass_regression",
                            "base_environment_failure",
                            "patch_pass",
                            "patch_fail",
                            "invalid",
                            "weak",
                            "not_run",
                        ],
                    },
                    "command": {"type": "string"},
                    "observed": {"type": "boolean"},
                    "evidence": {"type": "string"},
                    "failure_signature": {"type": "string"},
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
{goal}

{rules}"""
CONTRACT_MINER_PROMPT = """\
You are the read-only Contract Miner. Extract behavior contracts grounded in
the issue, source, public docs, or public tests. Never infer hidden assertions.
Also produce a concise behavior coverage matrix before coding. Always include
the reported problem path. Add only adjacent cases directly supported by public
source, tests, or issue text; do not broaden the requested behavior merely to
fill the matrix. Every case must cite contract ids and a public source path. A
suggested probe is a plan, not a claim that it ran. Use at most five cases.

Goal:
{goal}

Localization:
{localization}

{rules}"""
TEST_CARTOGRAPHER_PROMPT = """\
You are the read-only Test Cartographer. Identify relevant public tests,
fixtures, assertion style, and candidate test commands from repository evidence.
You also have Bash: use it to check the actual availability of those commands,
their required packages, and relevant runtime capabilities such as network.
Record commands actually run and facts observed as short strings in the
corresponding lists. Do not edit files or infer a framework-specific runner
from anything outside the public repository. Mark untested or inconclusive
facts in diagnostics.
Tools are executable commands; dependencies are runtime or project libraries;
capabilities are permissions or services such as network, package installation,
writable temporary storage, or databases. Record only task-relevant observed
facts; do not guess.
Keep the structured output concise and non-redundant. Each list item must
contain one fact only, and each fact must appear in only one field. The
runner_commands list contains exact executable public test commands only, with
no commentary, status, Git command, file-listing command, version check, or
other diagnostic command. Put observations and command results in diagnostics.
Put a test command in working_runner_commands only after it reached the intended
test runner; the tests themselves may pass or fail.

Goal:
{goal}

Localization:
{localization}

{rules}"""
PRE_VALIDATION_FACTORY_PROMPT = """\
You are the read-only Pre-Patch Validation Factory. Propose short public
evidence-backed probes linked to contract ids. Do not edit or run them. Mark
weak probes and abstain when evidence is insufficient.

Goal:
{goal}

Contracts:
{contracts}

Test cartography:
{cartography}

{rules}"""
JUDGE_PROMPT = """\
You are the read-only Validation Judge for {stage}. Accept at most {cap}
public-evidence probes. Reject missing contract ids, unsupported assertions,
implementation-derived or hidden-grader claims.

Goal:
{goal}

Contracts:
{contracts}

Candidates:
{candidates}

Test cartography:
{cartography}

{rules}"""
BASELINE_TRIAGE_PROMPT = """\
You are the Baseline Executor. Run accepted cheap probes and record exact
commands. Classify each as base_fail_repro, base_pass_regression,
base_environment_failure, invalid, weak, or not_run. Set observed=true only when the stated command actually executed
and produced the recorded result. A static inference, unavailable command, or
unexecuted proposal must be not_run and observed=false; it must never become
base_fail_repro or base_pass_regression. Keep temporary files outside the patch.
Use base_environment_failure only when an executed pre-patch command reaches a
real tool or runner and then fails because of an observed missing dependency,
tool, service, network capability, or setup condition. Record a short stable
failure_signature. Do not classify an assertion failure as environmental.

Goal:
{goal}

Accepted validation:
{judge}

Test cartography:
{cartography}

{rules}"""
CODER_PROMPT = (
    "You are the Coder, the only Council role authorized to change the task workspace.\n"
    "\n"
    "## Mission\n"
    "\n"
    "Implement a general, codebase-consistent source fix for the public issue using the evidence package below.\n"
    "Do not treat a nonempty diff as success: inspect the implementation, run relevant public "
    "validation when feasible, and leave a clean, intentional source diff for the evaluator.\n"
    "\n"
    "## Workspace and boundaries\n"
    "\n"
    "- The task repository is the workspace path stated in the shared rules below.\n"
    "- Modify only regular source files that are necessary for the fix.\n"
    "- Never modify or delete existing test files, and never modify configuration, packaging, installation, "
    "build, or setup files unless the public issue itself requires that exact change.\n"
    "- You may create a focused reproduction only under `/tmp/opencollab-validation-*`; remove it when "
    "finished and never leave it in the repository diff.\n"
    "- Never inspect hidden tests, benchmark harnesses, grader patches, FAIL_TO_PASS identifiers, caches, "
    "or other non-public evaluation material.\n"
    "- Do not download packages, fetch remote code, or assume network access; use the project dependencies "
    "and runner already present in the container.\n"
    "- Do not run `git commit`, `git reset`, `git checkout`, `git restore`, `git clean`, `git stash`, or any "
    "command that discards or rewrites the working tree.\n"
    "\n"
    "## Available tools and execution protocol\n"
    "\n"
    "- Use `grep` to find relevant public source, test, and documentation paths.\n"
    "- Use `file_read` to inspect the exact paths returned by `grep` or named in the evidence package.\n"
    "- Use `bash` for non-interactive project commands and the exact public test runner recorded by Test Cartography.\n"
    "- Use `file_write` only for one small, unambiguous replacement, and use `apply_patch` with raw `---`, "
    "`+++`, and `@@` unified-diff text for multi-site edits.\n"
    "- Use `git_diff` before finishing to inspect every remaining tracked change.\n"
    "- While work remains, every response MUST contain at least one actual function call through the tool "
    "interface; prose that describes a command does not execute it.\n"
    "- You may call multiple independent tools in one response, but inspect their results before issuing "
    "dependent calls.\n"
    "- A final response without tool calls ends this Coder session, so do not send it until implementation, "
    "validation, and diff review are complete.\n"
    "- Commands run in separate non-interactive shells; include required directory changes and environment "
    "setup in each command.\n"
    "\n"
    "## Required workflow\n"
    "\n"
    "1. Read the localized implementation and the closest public tests or documentation before deciding on a change.\n"
    "2. Use the supplied contracts, test cartography, and baseline triage as evidence, not as a substitute "
    "for inspecting the code path yourself.\n"
    "3. Prioritize the reported behavior and high-priority matrix cases. Treat other suggested cases as advisory, "
    "and do not broaden product behavior merely to satisfy a proposed probe.\n"
    "4. Reproduce the reported behavior or run the smallest relevant public test when the recorded runner "
    "is available.\n"
    "5. Implement the narrowest source change that fixes the root cause and preserves unaffected behavior.\n"
    "6. Run the relevant public test or focused reproduction after editing, followed by a broader related "
    "test when practical.\n"
    "7. If a public test fails after your edit, do not call it an expected future-test or harness outcome; "
    "either fix the concrete failure or state the exact command and unresolved result in the final response.\n"
    "8. Inspect `git_diff` and remove temporary artifacts before ending; the final diff must contain only "
    "intended source changes for this issue.\n"
    "\n"
    "## Completion\n"
    "\n"
    "The evaluator automatically captures the final working-tree diff after this role ends.\n"
    "Do not create `patch.txt`, do not use a submit command, and do not commit the change.\n"
    "Finish with a concise summary of the source change and the exact validation command and result, or "
    "explicitly say which validation could not run and why.\n"
    "\n"
    "Goal:\n"
    "{goal}\n"
    "\n"
    "Localization:\n"
    "{localization}\n"
    "\n"
    "Contracts:\n"
    "{contracts}\n"
    "\n"
    "Test cartography:\n"
    "{cartography}\n"
    "\n"
    "Pre-patch validation:\n"
    "{pre_judge}\n"
    "\n"
    "Baseline triage:\n"
    "{baseline_triage}\n"
    "{feedback_block}\n"
    "\n"
    "{rules}"
)
FEEDBACK_BLOCK = """
Previous attempt feedback:
{feedback}"""


def _read_tools() -> list[Any]:
    return toolset("file_read", "grep")


def _cartographer_tools() -> list[Any]:
    return toolset("bash", "file_read", "grep")


def _coder_tools() -> list[Any]:
    return toolset("bash", "file_read", "file_write", "apply_patch", "grep", "git_diff")


def _tester_tools() -> list[Any]:
    # The integrated verifier needs executable probes so PASS is backed by a
    # real run. It remains read-only and cannot author the patch.
    return verifier_toolset("bash", "file_read", "grep", "git_diff")


def _risk_tools() -> list[Any]:
    # The critic reviews the verifier's complete evidence in one structured response.
    return []


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


def _contracts_brief(value: dict[str, Any], limit: int = 6000) -> str:
    contracts = []
    for item in _items(value.get("contracts"), 5):
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
    coverage_matrix = []
    for item in _items(value.get("coverage_matrix"), 5):
        if isinstance(item, dict):
            coverage_matrix.append(
                {
                    "case_id": _clip(item.get("case_id"), 80),
                    "contract_ids": [
                        _clip(ref, 80) for ref in _items(item.get("contract_ids"), 5)
                    ],
                    "dimensions": _clip(item.get("state_dimensions"), 180),
                    "expected": _clip(item.get("expected_behavior"), 180),
                    "path": _clip(item.get("relevant_source_path"), 140),
                    "priority": item.get("priority"),
                    "probe": _clip(item.get("suggested_probe"), 220),
                }
            )
    return _bounded_dump(
        {"contracts": contracts, "coverage_matrix": coverage_matrix},
        limit,
    )


def _cartography_brief(value: dict[str, Any]) -> str:
    return _bounded_dump(
        {
            "framework": value.get("framework", ""),
            "working_runner_commands": value.get("working_runner_commands", []),
            "runner_commands": value.get("runner_commands", []),
            "test_files": value.get("test_files", []),
            "fixtures": value.get("fixtures", []),
            "assertion_style": value.get("assertion_style", ""),
            "available_tools": value.get("available_tools", []),
            "unavailable_tools": value.get("unavailable_tools", []),
            "available_dependencies": value.get("available_dependencies", []),
            "unavailable_dependencies": value.get("unavailable_dependencies", []),
            "capabilities": value.get("capabilities", []),
            "diagnostics": value.get("diagnostics", []),
            "guidance": value.get("temporary_test_guidance", ""),
        },
        6000,
    )


def _candidates_brief(value: dict[str, Any], cap: int, limit: int = 400) -> str:
    tests = []
    for item in value.get("tests", []):
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


def _accepted_validation_package(
    candidates: dict[str, Any],
    judge: dict[str, Any],
) -> str:
    """Join accepted decisions back to their complete proposed probes by ID."""
    proposals = {
        str(item.get("id")): item
        for item in candidates.get("tests", [])
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    accepted = []
    for decision in judge.get("accepted", []):
        if not isinstance(decision, dict):
            continue
        test_id = str(decision.get("id") or "")
        proposal = proposals.get(test_id)
        accepted.append(
            {
                "decision": decision,
                "proposal": proposal,
                "proposal_missing": proposal is None,
            }
        )
    return _dump(
        {
            "accepted": accepted,
            "validation_brief": judge.get("validation_brief", ""),
        }
    )


def _triage_brief(value: dict[str, Any], limit: int = 12_000) -> str:
    classifications = []
    for item in value.get("classifications", []):
        if isinstance(item, dict):
            classifications.append(
                {
                    "id": _clip(item.get("test_id"), 80),
                    "status": _clip(item.get("status"), 80),
                    "observed": bool(item.get("observed")),
                    "command": _clip(item.get("command"), 160),
                    "failure_signature": _clip(item.get("failure_signature"), 140),
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


def _is_pass(verdict: Any) -> bool:
    return isinstance(verdict, dict) and verdict.get("verdict") == "PASS"


def _is_blocked(verdict: Any) -> bool:
    return isinstance(verdict, dict) and verdict.get("verdict") == "BLOCKED"


async def _source_diff_present(ctx: Any, exclude_paths: list[str]) -> bool | None:
    source_changed = getattr(ctx, "source_changed", None)
    if source_changed is None:
        return None
    return await source_changed(exclude_paths)
