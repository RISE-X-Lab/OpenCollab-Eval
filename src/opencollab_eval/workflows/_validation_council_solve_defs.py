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
from typing import Any

from opencollab.sdk.tools import (
    ApplyPatchTool,
    BashTool,
    FileReadTool,
    FileWriteTool,
    GitDiffTool,
    GrepTool,
    verification_run_tests_tool,
)

MAX_APPROVED_PRE_TESTS = 5
MAX_APPROVED_POST_TESTS = 4
WORKFLOW_VARIANT = "G1.1"
MAX_CODER_ROUNDS = 3
LOCALIZER_BUDGET = 220_000
EVIDENCE_BUDGET = 180_000
VALIDATION_FACTORY_BUDGET = 160_000
JUDGE_BUDGET = 100_000
TRIAGE_BUDGET = 180_000
RISK_BUDGET = 60_000
VERIFIER_BUDGET = 220_000
STRUCTURED_ROLE_TIMEOUT_SECONDS = 300
CODER_ROLE_TIMEOUT_SECONDS = 1800
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
- Use only the issue text, repository code, public tests, and public docs.
- Do not use hidden grader tests, official test patches, injected FAIL_TO_PASS
  node ids, or any task extra that reveals the grading suite.
- Prefer dedicated tools: file_read/grep for inspection, run_tests for tests,
  file_write/apply_patch for edits. Use bash only when no dedicated tool fits.
- Keep temporary validation outside the final diff. Do not edit tests unless the
  task explicitly asks for a test-only change.
- Only roles with command or write tools may create temporary validation files.
  If your current tools cannot create or run a probe, report that limitation in
  structured_output instead of searching for unavailable tools.
- If a validation probe needs a temporary file and your role has a tool that can
  create it, write it only under /tmp/opencollab-validation-* and remove it
  after use.
- Fix the source root cause with the smallest correct change.
- Never run git commit; leave edits in the working tree."""

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
You are the Analyst / Localizer. Analyze only; do not edit files.
Identify the likely source area, public API, root-cause hypothesis, unknowns,
and definition of done. Read the repository and public tests for evidence.

{rules}

Goal:
{goal}"""

CONTRACT_MINER_PROMPT = """\
You are the Contract Miner. Extract behavior contracts only. A contract must be
grounded in issue text, source behavior, public docs, or public tests. Do not
write tests and do not infer exact hidden assertions.

For each contract, record whether it describes desired behavior, currently
buggy behavior, or existing unaffected behavior. Cite concrete evidence.

{rules}

Goal:
{goal}

Localization:
{localization}"""

TEST_CARTOGRAPHER_PROMPT = """\
You are the Test Cartographer. Map how this repository expresses tests: runner,
fixtures, assertion style, relevant public test files, and how temporary probes
can be run without entering the final diff. Do not solve the issue.

{rules}

Goal:
{goal}

Localization:
{localization}"""

PRE_VALIDATION_FACTORY_PROMPT = """\
You are the Pre-Patch Validation Factory. Propose candidate validation probes
before coding. Each candidate must link to behavior contract ids and evidence.
Prefer short repro, boundary, regression, or metamorphic probes. Mark weak or
diagnostic-only probes as such. Do not edit files, do not run probes, and do not
look for write tools. If evidence is insufficient, set abstained=true.

{rules}

Goal:
{goal}

Localization:
{localization}

Contracts:
{contracts}

Test cartography:
{cartography}"""

JUDGE_PROMPT = """\
You are the Validation Judge / Prioritizer for the {stage} stage. Apply hard
evidence gates. Accept at most {cap} candidates. Reject a candidate if it lacks
contract ids, lacks concrete evidence, asserts behavior only from a proposed
implementation, or depends on hidden grader knowledge. Diagnostics may be kept
separate, but they must not block final acceptance. Do not edit files, do not
create temporary probes, and do not look for write tools.

{rules}

Goal:
{goal}

Contracts:
{contracts}

Candidates:
{candidates}"""

BASELINE_TRIAGE_PROMPT = """\
You are the Baseline Executor and Triage role. Run only accepted validation
probes that are cheap and safe, using temporary files or one-shot commands that
do not enter the final diff. If a file is needed, use /tmp/opencollab-validation-*
only when the provided tools can create it. If the provided tools cannot run a
probe exactly, classify it as not_run or weak instead of searching for missing
tools. Classify each accepted probe against the current base as base_fail_repro,
base_pass_regression, invalid, weak, or not_run. Record exact commands and
observations.

{rules}

Goal:
{goal}

Accepted validation:
{judge}"""

CODER_PROMPT = """\
You are the Coder. Implement a minimal source fix using the evidence package.
Do not edit tests unless the task explicitly requires test-only changes.
Run relevant public tests and accepted validation probes where practical.
Once you can name the concrete source change, stop reading and call file_write
or apply_patch in that turn. Do not announce an edit and then call file_read or
grep.
This is a SWE-bench patch candidate: a clean working tree is a failed attempt.
If the issue appears already fixed, still identify the source delta required by
the task and leave a minimal tracked source diff. Do not submit "no change
needed" as the fix.
Your final message should name changed source files, explain the root cause,
and summarize verification.

{rules}

Goal:
{goal}

Localization:
{localization}

Contracts:
{contracts}

Test cartography:
{cartography}

Pre-patch validation judge:
{pre_judge}

Baseline triage:
{baseline_triage}
{feedback_block}"""

FEEDBACK_BLOCK = """
Previous attempt feedback:
{feedback}"""

PATCH_VALIDATOR_PROMPT = """\
You are the Patch Validator. Do not edit files. Verify the current working tree
against the goal, public tests, accepted pre-patch validation, and baseline
triage. Run existing tests when practical; do not create new probe files and do
not search for write tools. If a requested probe cannot be run with available
tools, say so in findings. Verdict PASS only when the source change is present,
minimal, and satisfies the approved validation. Run `git diff --name-only`; put
legitimate source paths in allowed_patch_paths and all tests, temporary probes,
caches, logs, notes, and generated artifacts in disallowed_patch_paths.
If `git diff --name-only` is empty, Verdict must be FAIL. Do not PASS a clean
working tree on the theory that the checkout already contains the fix.
Do not require repository test files to be edited for a PASS; in this harness,
tests are validation artifacts unless the task is explicitly test-only.

{rules}

Goal:
{goal}

Coder report:
{coder_report}

Accepted validation:
{pre_judge}

Baseline triage:
{baseline_triage}"""

DIFF_RISK_PROMPT = """\
You are the Diff Risk Auditor. Do not edit files and do not run tools. Use only
the contracts and patch validator verdict already provided below. Identify at
most three semantic risks, missed contracts, neighboring behavior that may
regress, and focused probes that would catch those risks. If the verdict already
contains enough clean evidence, return an empty risks list with a concise
summary. Do not inspect more repository files. Your next action must be
structured_output.

{rules}

Goal:
{goal}

Contracts:
{contracts}

Patch validator verdict:
{patch_verdict}"""

POST_VALIDATION_FACTORY_PROMPT = """\
You are the Post-Patch Validation Factory. Use the accepted contracts, current
diff risks, and public repository behavior to propose additional post-patch
probes. Do not derive assertions only from the implementation. Do not edit
files, do not run probes, and do not look for write tools. If risks are empty or
evidence is insufficient, set abstained=true.

{rules}

Goal:
{goal}

Contracts:
{contracts}

Diff risks:
{risks}"""

POST_TRIAGE_PROMPT = """\
You are the Post-Patch Validation Triage role. Run accepted post-patch probes
when cheap and safe. Keep temporary probes outside the final diff. Classify each
probe as patch_pass, patch_fail, invalid, weak, or not_run. If a file is needed,
use /tmp/opencollab-validation-* only when the provided tools can create it. If
the provided tools cannot run a probe exactly, classify it as not_run or weak
instead of searching for missing tools. Report exact commands and observations.

{rules}

Goal:
{goal}

Accepted post-patch validation:
{judge}"""

FINAL_VERIFIER_PROMPT = """\
You are the Final Verifier. Do not edit files. Inspect git diff, run relevant
public tests and approved validation where practical, and check that temporary
validation files are absent from the final diff. Run existing tests when
practical; do not create new probe files and do not search for write tools. Run
`git diff --name-only` and place legitimate source changes in allowed_patch_paths.
Place all tests, temporary probes, caches, logs, notes, and generated artifacts
in disallowed_patch_paths, and fail if any disallowed path remains in the diff.
Verdict PASS only when the issue is fixed by source changes and the validation
evidence is clean.
If `git diff --name-only` is empty, Verdict must be FAIL. Never accept "already
fixed in this checkout" as a PASS for a SWE-bench submission.
Do not fail a source patch solely because repository test files were not edited;
tests are validation artifacts in this harness unless the task is explicitly
test-only.

{rules}

Goal:
{goal}

Localization:
{localization}

Contracts:
{contracts}

Pre-patch validation:
{pre_judge}

Baseline triage:
{baseline_triage}

Coder report:
{coder_report}

Patch validator verdict:
{patch_verdict}

Diff risks:
{risks}

Post-patch validation:
{post_judge}

Post-patch triage:
{post_triage}"""


def _read_tools() -> list[Any]:
    return [FileReadTool(), GrepTool()]


def _coder_tools() -> list[Any]:
    return [
        BashTool(),
        FileReadTool(),
        FileWriteTool(),
        ApplyPatchTool(),
        verification_run_tests_tool(),
        GrepTool(),
    ]


def _tester_tools() -> list[Any]:
    # Gate roles (patch-validator, post-triage, final-verifier) need an executable
    # probe so a PASS is backed by a real run, not prose alone. Blindness holds
    # because the hidden FAIL_TO_PASS tests are absent from the container, not
    # because bash is. No file_write/apply_patch: these roles verify, not author.
    return [
        BashTool(),
        FileReadTool(),
        verification_run_tests_tool(),
        GrepTool(),
        GitDiffTool(),
    ]


def _risk_tools() -> list[Any]:
    # The diff-risk auditor must at least read the diff and the sources it judges;
    # an empty toolset let it "audit" blind. Read-only — no execution or authoring.
    return [FileReadTool(), GrepTool(), GitDiffTool()]


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


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
    parts: list[str] = []
    for report in reports:
        if isinstance(report, dict):
            text = report.get("findings") or report.get("approved_brief") or report.get("summary")
            if text:
                parts.append(str(text))
        elif isinstance(report, str) and report.strip():
            parts.append(report.strip())
    return "\n\n".join(parts) or "No structured feedback was returned; re-verify from the evidence package."
