"""swe-committee-v2 - committee-style SWE workflow with explicit stage boundaries.

This workflow is implemented from the requested committee graph:
Analyst/Localizer -> Evidence Stage -> Contract Tribunal -> Pre-patch
validation -> Baseline triage -> Coder -> Existing Tests + Approved Validation
-> Patch Attack Stage -> Post-patch validation -> Final skeptic -> Final
verifier, with bounded Coder Minimal Retry rounds.
"""

from __future__ import annotations

from typing import Any

MAX_PRE_TESTS = 5
MAX_POST_TESTS = 4
MAX_CODER_ROUNDS = 3


EMPTY_POST_CANDIDATES = {
    "tests": [],
    "abstained": True,
    "rationale": "Post-patch validation skipped.",
}
EMPTY_POST_JUDGE = {
    "verdict": "PASS",
    "accepted": [],
    "rejected": [],
    "diagnostic": [],
    "validation_brief": "Post-patch validation skipped.",
    "triage": [],
    "retry_feedback": "",
}
EMPTY_DIFF_RISKS = {
    "risks": [],
    "summary": "No diff-risk signal emitted.",
    "evidence": [],
}
EMPTY_SKEPTIC = {
    "verdict": "PASS",
    "findings": "No skeptic signal.",
    "required_evidence": [],
    "next_action": "Retry only on explicit contract or verifier blockers.",
}

EMPTY_FINAL_VERIFIER = {
    "verdict": "CONCRETE_BLOCKER",
    "findings": "Final verifier returned no structured verdict.",
    "allowed_patch_paths": [],
    "disallowed_patch_paths": [],
    "retry_feedback": "Re-check the patch against approved validation and diff boundaries.",
}


SHARED_RULES = """\
Rules:
- Use only issue text, repository source, public tests, and public docs.
- Do not use hidden grader test patches, injected FAIL_TO_PASS IDs, or any hidden suite.
- Prefer file_read/grep for inspection and run_tests for validation checks.
- Use bash only when no dedicated tool fits.
- Never include temporary validation artifacts or test scripts in final patch output.
- Keep temporary validation outside git diff; remove temporary validation files before final verifier pass.
- Minimize edits: prefer smallest behavior-preserving patch.
- Never run git commit.
"""

LOCALIZATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "summary",
        "root_cause_hypothesis",
        "scope_files",
        "public_api",
        "uncertainties",
        "definition_of_done",
    ],
    "properties": {
        "summary": {"type": "string"},
        "root_cause_hypothesis": {"type": "string"},
        "scope_files": {"type": "array", "items": {"type": "string"}},
        "public_api": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "definition_of_done": {"type": "string"},
    },
}

CONTRACT_MINER_SCHEMA: dict[str, Any] = {
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
                    "behavior_kind",
                    "evidence",
                    "confidence",
                    "testability",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "statement": {"type": "string"},
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
        }
    },
}

TEST_CARTOGRAPHY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "framework",
        "runner_commands",
        "test_files",
        "fixtures",
        "temporary_test_guidance",
    ],
    "properties": {
        "framework": {"type": "string"},
        "runner_commands": {"type": "array", "items": {"type": "string"}},
        "test_files": {"type": "array", "items": {"type": "string"}},
        "fixtures": {"type": "array", "items": {"type": "string"}},
        "temporary_test_guidance": {"type": "string"},
    },
}

OBSERVABLE_INVENTORY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "observable_fields",
        "unaffected_fields",
        "risky_fields",
        "notes",
    ],
    "properties": {
        "observable_fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Potentially observable behavior outputs, exception shapes, return values, ordering.",
        },
        "unaffected_fields": {"type": "array", "items": {"type": "string"}},
        "risky_fields": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
}

TRIBUNAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "contracts",
        "strong_contract_ids",
        "weak_contract_ids",
        "hypothesis_contract_ids",
        "forbidden_fields",
        "rationale",
    ],
    "properties": {
        "contracts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "statement",
                    "evidence_tier",
                    "evidence",
                    "contract_type",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "statement": {"type": "string"},
                    "evidence_tier": {
                        "type": "string",
                        "enum": ["strong", "weak", "speculative", "forbidden"],
                    },
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "contract_type": {
                        "type": "string",
                        "enum": ["desired", "current_buggy", "existing_unaffected"],
                    },
                },
            },
        },
        "strong_contract_ids": {"type": "array", "items": {"type": "string"}},
        "weak_contract_ids": {"type": "array", "items": {"type": "string"}},
        "hypothesis_contract_ids": {"type": "array", "items": {"type": "string"}},
        "forbidden_fields": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
}

BASELINE_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "accepted", "rejected", "diagnostic", "validation_brief"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL", "BLOCKED"]},
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
        "rejected": {"type": "array", "items": {"type": "object"}},
        "diagnostic": {"type": "array", "items": {"type": "string"}},
        "validation_brief": {"type": "string"},
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
                    "expected_on_base": {
                        "type": "string",
                        "enum": ["fail", "pass", "unknown"],
                    },
                    "expected_on_patch": {
                        "type": "string",
                        "enum": ["pass", "unknown"],
                    },
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
                        "enum": ["base-fail", "base-pass", "patch-pass", "patch-fail", "invalid", "weak", "not_run"],
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
    "required": ["risks", "summary", "evidence"],
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
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
}

ETV_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "status",
        "findings",
        "checks",
        "allowed_patch_paths",
        "disallowed_patch_paths",
    ],
    "properties": {
        "status": {"type": "string", "enum": ["PASS", "FAIL", "NOT_RUN"]},
        "findings": {"type": "string"},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "status", "evidence"],
                "properties": {
                    "name": {"type": "string"},
                    "status": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        },
        "allowed_patch_paths": {"type": "array", "items": {"type": "string"}},
        "disallowed_patch_paths": {"type": "array", "items": {"type": "string"}},
    },
}

POST_VALIDATION_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "verdict",
        "accepted",
        "rejected",
        "diagnostic",
        "triage",
        "validation_brief",
        "retry_feedback",
    ],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL_WITH_EVIDENCE"]},
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
        "rejected": {"type": "array", "items": {"type": "object"}},
        "diagnostic": {"type": "array", "items": {"type": "string"}},
        "triage": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["test_id", "status", "evidence"],
                "properties": {
                    "test_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["patch-pass", "patch-fail", "invalid", "weak", "not_run"],
                    },
                    "evidence": {"type": "string"},
                },
            },
        },
        "validation_brief": {"type": "string"},
        "retry_feedback": {"type": "string"},
    },
}

SKEPTIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "findings", "required_evidence", "next_action"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "CONTRACT_EVIDENCE_BLOCKER"]},
        "findings": {"type": "string"},
        "required_evidence": {"type": "array", "items": {"type": "string"}},
        "next_action": {"type": "string"},
    },
}

FINAL_VERIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "verdict",
        "findings",
        "allowed_patch_paths",
        "disallowed_patch_paths",
        "retry_feedback",
    ],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "CONCRETE_BLOCKER"]},
        "findings": {"type": "string"},
        "allowed_patch_paths": {"type": "array", "items": {"type": "string"}},
        "disallowed_patch_paths": {"type": "array", "items": {"type": "string"}},
        "retry_feedback": {"type": "string"},
    },
}


LOCALIZER_PROMPT = """\
You are Analyst / Localizer.
Read only. Build the root-cause map for one SWE issue: likely files, public API
touchpoints, hypothesis and uncertainties, and a precise definition of done.

{rules}

Goal:
{goal}"""

CONTRACT_MINER_PROMPT = """\
You are the Contract Miner.
Extract minimal behavior contracts from issue text, source, public tests, and docs.
Each contract must be evidence-backed and tagged by behavior kind.

{rules}

Goal:
{goal}

Localization:
{localization}"""

TEST_CARTOGRAPHER_PROMPT = """\
You are the Test Cartographer.
Map how this repository exposes observable behavior: test framework, runner
commands, relevant public test files, and temporary validation scaffolding.

{rules}

Goal:
{goal}

Localization:
{localization}"""

CONTRACT_INVENTORY_PROMPT = """\
You are Observable Contract Inventory.
List concrete externally visible fields and interactions for this issue context:
return values, exceptions, warnings/messages, ordering, defaults, and public side
effects that should be preserved.

{rules}

Goal:
{goal}

Localization:
{localization}"""

CONTRACT_TRIBUNAL_PROMPT = """\
You are Contract Tribunal.
Merge Contract Miner, Test Cartographer, and Observable Contract Inventory into a
single evidence protocol.

- Produce a strong contract set with direct evidence and high confidence.
- Produce a weak set from one-line or indirect evidence.
- Keep a hypothesis set only when clearly unresolved.
- Explicitly list forbidden fields: anything you have insufficient evidence to
  assert as stable.
- Do not keep guessed behavior in strong contracts.

If the previous attempt had blockers, incorporate them and downgrade speculative
items first.

{rules}

Goal:
{goal}

Localization:
{localization}

Contract Miner:
{contract_miner}

Test Cartographer:
{test_cartographer}

Observable Inventory:
{contract_inventory}

Previous Arbitration Notes:
{feedback}"""

CANDIDATE_GENERATION_PROMPT = """\
You are the Pre-Patch Validation Factory.
Propose strong validation probes for the most likely root-cause.

Do not emit probes that rely on hidden grader tests or guessed internals.
Link each probe to valid contract ids.

{rules}

Goal:
{goal}

Localization:
{localization}

Contract Tribunal:
{tribunal}

Test Cartography:
{test_cartography}"""

BASELINE_JUDGE_PROMPT = """\
You are Validation Judge / Prioritizer for Pre-Patch stage.
Reject weak proposals quickly. Accept at most {cap} candidates with clear contract
coverage and reproducible evidence.

{rules}

Goal:
{goal}

Contracts (from Tribunal):
{tribunal}

Candidates:
{candidates}"""

BASELINE_TRIAGE_PROMPT = """\
You are Baseline Executor + Triage.
Run accepted probes cheaply. Classify each as base-fail, base-pass, patch-fail,
invalid, weak, or not_run. Report command evidence and classify clearly.

{rules}

Goal:
{goal}

Baseline candidates:
{judge}"""

CODER_PROMPT = """\
You are the Coder.
Implement a minimal source fix consistent with the localization, contracts, and tests.
Do not edit tests unless explicitly required.
Run practical focused checks and keep temporary validation files outside final diff.

{rules}

Goal:
{goal}

Localization:
{localization}

Contract Tribunal:
{tribunal}

Test Cartography:
{cartography}

Pre-patch judge:
{pre_judge}

Baseline triage:
{baseline_triage}

{feedback_block}
"""

ETV_PROMPT = """\
You are Existing Tests + Approved Validation.
Run the existing tests and the approved validation checks that are practical for
this patch. Record observed results and patch path boundaries.

Classify allowed paths (source fixes only) and disallowed paths (temporary files,
tests, caches, logs, notes, artifacts).

{rules}

Goal:
{goal}

Coder report:
{coder_report}

Pre-patch judge:
{pre_judge}

Baseline triage:
{baseline_triage}"""

DIFF_RISK_BRANCH_PROMPT = """\
You are Branch / Boundary Attack.
Use git diff and temporary reasoning to enumerate behavior-risky boundary and branch
changes, and concrete checks that would catch missed corner cases.

{rules}

Goal:
{goal}

Contracts:
{tribunal}

Existing Tests + Approved Validation:
{patch_verdict}"""

DIFF_RISK_REGRESSION_PROMPT = """\
You are Regression Impact Scan.
From contract and diff context, identify likely regressions in neighboring behavior.

{rules}

Goal:
{goal}

Contracts:
{tribunal}

Existing Tests + Approved Validation:
{patch_verdict}"""

DIFF_RISK_OBSERVABLE_PROMPT = """\
You are Observable Diff Review.
Read the current diff and identify changed observable fields that lack direct contract
support from pre-existing evidence.

{rules}

Goal:
{goal}

Contract Inventory:
{inventory}

Existing Tests + Approved Validation:
{patch_verdict}"""

POST_VALIDATION_FACTORY_PROMPT = """\
You are Post-Patch Validation Factory.
Propose targeted post-patch probes for strongest remaining risks.

{rules}

Goal:
{goal}

Contract Tribunal:
{tribunal}

Diff risks:
{diff_risks}"""

POST_JUDGE_TRIAGE_PROMPT = """\
You are Post Validation Judge + Triage.
Reject probes that encode guesses as expected behavior. Accept at most {cap}
evidence-backed probes, run the accepted checks when practical, and triage the
results.

Return FAIL_WITH_EVIDENCE only when an accepted evidence-backed check clearly
fails on the current patch. Return PASS for invalid, weak, diagnostic-only, or
not-run checks.

{rules}

Goal:
{goal}

Contracts:
{tribunal}

Candidates:
{candidates}

Existing Tests + Approved Validation:
{etv_report}"""

FINAL_SKEPTIC_PROMPT = """\
You are Final Skeptic.
Return CONTRACT_EVIDENCE_BLOCKER only when the patch depends on an unsupported
contract claim. Examples: claims over weak/hypothetical contracts, changes to
fields not authorized by the tribunal, or un-audited side effects without
evidence.

Return PASS for concrete patch problems that can be sent directly to the coder.

{rules}

Goal:
{goal}

Localization:
{localization}

Contract Tribunal:
{tribunal}

Existing Tests + Approved Validation:
{etv_report}

Post validation decision:
{post_decision}

Diff risks:
{risks}"""

FINAL_VERIFIER_PROMPT = """\
You are Final Verifier.
Confirm issue fix, minimal source change, and clean diff boundaries.
Return PASS only when the patch is ready as the final model_patch.
Return CONCRETE_BLOCKER for a concrete fix/test/diff issue that the Coder Minimal
Retry can act on without re-opening contract arbitration.

{rules}

Goal:
{goal}

Localization:
{localization}

Contract Tribunal:
{tribunal}

Existing Tests + Approved Validation:
{etv_report}

Diff risks:
{risks}

Post validation:
{post_decision}

Skeptic:
{skeptic}"""
