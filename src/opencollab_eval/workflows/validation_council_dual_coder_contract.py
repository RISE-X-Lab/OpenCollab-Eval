"""Keep the G21 workflow identity while sharing OpenCollab's coder execution."""

from __future__ import annotations

from typing import Any

from opencollab.builtin_workflows import CONTRACT_PROMPT, run_dual_coder
from opencollab.workflows import workflow


@workflow(
    name="validation-council-dual-coder-contract-v1",
    description="Two autonomous coder strategies with public contract adjudication",
    phases=["minimal-coder", "cross-component-coder", "mechanical-selection", "contract-adjudication", "adoption"],
)
async def validation_council_dual_coder_contract_v1(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Compare minimal and cross-component autonomous repair strategies."""
    return await run_dual_coder(ctx, args, selector_prompt=CONTRACT_PROMPT)


__all__ = ["validation_council_dual_coder_contract_v1"]
