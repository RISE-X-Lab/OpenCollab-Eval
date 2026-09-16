"""Independent counterexample critic for the validation council workflow."""

from __future__ import annotations

from typing import Any

RISK_CRITIC_BUDGET = 80_000

RISK_CRITIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["recommendation", "risks", "summary"],
    "properties": {
        "recommendation": {
            "type": "string",
            "enum": ["PASS", "RETRY"],
        },
        "risks": {
            "type": "array",
            "maxItems": 2,
            "items": {
                "type": "object",
                "required": [
                    "risk",
                    "contract_ids",
                    "source_path_or_unknown",
                    "distinguishing_probe",
                    "evidence",
                ],
                "properties": {
                    "risk": {"type": "string"},
                    "contract_ids": {"type": "array", "items": {"type": "string"}},
                    "source_path_or_unknown": {"type": "string"},
                    "distinguishing_probe": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        },
        "summary": {"type": "string"},
    },
}

RISK_CRITIC_PROMPT = """\
You are a concise, read-only Counterexample Critic. Review the supplied diff
and evidence once, without tools. Do not repeat tests or require every advisory
coverage case. Return RETRY only when the supplied source or executable evidence
shows a concrete defect. Give at most two risks, each with a source location and
a public distinguishing probe. Missing evidence alone is not a defect. Otherwise
return PASS. Do not invent hidden tests or environment claims.

Goal:
{goal}

Contracts:
{contracts}

Integrated verifier evidence:
{post_patch_evidence}

Deterministic patch facts:
{diff_facts}

{rules}"""


def normalize_risk_critic(value: Any) -> dict[str, Any]:
    """Normalize the compact critic report without turning absence into a defect."""
    if not isinstance(value, dict):
        return {
            "recommendation": "INCONCLUSIVE",
            "risks": [],
            "summary": "Risk critic returned no structured report.",
        }
    risks = value.get("risks")
    report = {
        "recommendation": str(value.get("recommendation") or "INCONCLUSIVE"),
        "risks": (
            [item for item in risks if isinstance(item, dict)][:2]
            if isinstance(risks, list)
            else []
        ),
        "summary": str(value.get("summary") or ""),
    }
    if report["recommendation"] not in {"PASS", "RETRY"}:
        report["recommendation"] = "INCONCLUSIVE"
    if report["recommendation"] == "PASS" and report["risks"]:
        report["recommendation"] = "RETRY"
    if report["recommendation"] == "RETRY" and not report["risks"]:
        report["recommendation"] = "INCONCLUSIVE"
    return report
