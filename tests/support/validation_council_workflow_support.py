"""Shared preparation for validation council workflow."""

from __future__ import annotations

from typing import Any


class ScriptedCtx:
    def __init__(self, replies: list[Any]) -> None:
        self._replies = list(replies)
        self.agent_calls: list[dict[str, Any]] = []
        self.phases: list[str] = []
        self.logs: list[str] = []

    def tokens_spent(self) -> int:
        return 123

    async def agent(self, prompt, *, schema=None, label=None, tools=None, isolation=False, **kwargs):
        self.agent_calls.append(
            {"prompt": prompt, "schema": schema, "label": label, "tools": tools, **kwargs}
        )
        return self._replies.pop(0)

    async def parallel(self, thunks):
        return [await thunk() for thunk in thunks]

    async def phase(self, title):
        self.phases.append(title)

    async def log(self, message):
        self.logs.append(message)



LOCALIZATION = {
    "summary": "empty widget crashes",
    "root_cause_hypothesis": "parse misses empty input",
    "files": ["widget.py"],
    "public_api": ["widget.parse"],
    "uncertainties": [],
    "definition_of_done": "empty input returns an empty widget",
}



CONTRACTS = {
    "contracts": [
        {
            "id": "C1",
            "statement": "empty input is accepted",
            "scope": "widget.parse",
            "behavior_kind": "desired",
            "evidence": [
                {
                    "source_type": "issue",
                    "file_or_section": "problem statement",
                    "summary": "user reports empty input crash",
                }
            ],
            "confidence": "medium",
            "testability": "direct function call",
        }
    ]
}



CARTOGRAPHY = {
    "framework": "pytest",
    "runner_commands": ["pytest tests/test_widget.py"],
    "test_files": ["tests/test_widget.py"],
    "fixtures": [],
    "assertion_style": "plain assert",
    "temporary_test_guidance": "use python -c probes",
}



CANDIDATES = {
    "tests": [
        {
            "id": "T1",
            "contract_ids": ["C1"],
            "type": "repro",
            "oracle_type": "return value",
            "setup": "call parse('')",
            "assertion": "returns empty widget",
            "expected_on_base": "fail",
            "expected_on_patch": "pass",
            "why_distinguishes_wrong_patch": "catches empty-input crash",
            "evidence_refs": ["C1"],
            "runner_command": "python -c \"import widget; widget.parse('')\"",
            "risk_of_false_positive": "low",
        }
    ],
    "abstained": False,
    "rationale": "direct repro",
}



JUDGE = {
    "accepted": [
        {"id": "T1", "priority": 1, "classification": "repro", "reason": "contract backed"}
    ],
    "rejected": [],
    "diagnostic": [],
    "validation_brief": "run T1 when cheap",
}



TRIAGE = {
    "classifications": [
        {"test_id": "T1", "status": "base_fail_repro", "evidence": "raises ValueError"}
    ],
    "approved_brief": "T1 is a valid repro",
    "abstained": False,
}



RISKS = {
    "risks": [
        {
            "id": "R1",
            "changed_area": "widget.parse",
            "risk": "None handling regresses",
            "contract_ids": ["C1"],
            "suggested_probe": "parse(None)",
            "priority": 1,
        }
    ],
    "summary": "small parser risk",
}



PASS = {
    "verdict": "PASS",
    "findings": "validated",
    "allowed_patch_paths": ["widget.py"],
    "disallowed_patch_paths": [],
}



FAIL = {
    "verdict": "FAIL",
    "findings": "edge case still fails",
    "allowed_patch_paths": ["widget.py"],
    "disallowed_patch_paths": [],
}



def _base_replies(final_verdict: dict[str, str] = PASS) -> list[Any]:
    return [
        LOCALIZATION,
        CONTRACTS,
        CARTOGRAPHY,
        CANDIDATES,
        JUDGE,
        TRIAGE,
        "changed widget.py",
        PASS,
        RISKS,
        CANDIDATES,
        JUDGE,
        TRIAGE,
        final_verdict,
    ]

