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

from typing import Any

from opencollab.workflows import workflow

from opencollab_eval.workflows import _validation_council_solve_defs as _definitions
from opencollab_eval.workflows import _validation_council_solve_impl as _implementation

_read_tools = _definitions._read_tools
_risk_tools = _definitions._risk_tools
_coder_tools = _definitions._coder_tools
_tester_tools = _definitions._tester_tools


@workflow(
    name="validation-council-solve",
    description=("Blind contract-led SWE workflow with validation judges, diff risk audit, and capped retry"),
    phases=[
        "localize",
        "evidence",
        "pre-validate",
        "solve",
        "diff-risk",
        "final-verify",
    ],
)
async def validation_council_solve(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Public-module workflow wrapper used by discovery."""
    return await _implementation.validation_council_solve(ctx, args)


_LEGACY_EXPORTS = tuple(
    sorted({name for module in (_definitions, _implementation) for name in dir(module) if not name.startswith("_")})
)
__all__ = list(_LEGACY_EXPORTS)


def __getattr__(name: str):
    """Preserve direct imports of legacy definition names."""
    if name in _LEGACY_EXPORTS:
        for module in (_definitions, _implementation):
            if hasattr(module, name):
                return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


for _export in _LEGACY_EXPORTS:
    for _module in (_definitions, _implementation):
        if hasattr(_module, _export):
            globals().setdefault(_export, getattr(_module, _export))
            break
