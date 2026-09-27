"""Keep the G21 workflow identity while sharing OpenCollab's coder execution."""

from __future__ import annotations

from typing import Any

from opencollab.builtin_workflows import run_dual_coder
from opencollab.workflows import workflow

from ._validation_council_solve_defs import SHARED_RULES

MINIMAL_CODER_PROMPT = """\
You are autonomous coder A. Work only in this isolated candidate worktree.

{rules}

Public issue
{goal}

Find the narrowest root cause that fully explains the issue. Preserve backward
compatibility and existing public behavior outside the requested change. Trace
the immediate callers and consumers needed to verify the fix, then implement a
minimal complete source patch. Avoid broad refactors, speculative cleanup, test
edits, generated files, caches, and logs. Run the nearest relevant public tests
with bash, inspect the final diff, and finish with a non-empty source diff.
Do not use official results, hidden tests, FAIL_TO_PASS ids, grader patches, or
historical outcomes."""


CROSS_COMPONENT_CODER_PROMPT = """\
You are autonomous coder B. Work only in this isolated candidate worktree.

{rules}

Public issue
{goal}

Shared public command observed from candidate A
{public_command}

Solve the issue end to end with emphasis on cross-component completeness.
Trace every producer and producing state or data path, every direct consumer, public API and
serialization contract, error propagation, lifecycle boundary, and relevant
edge cases. Implement the smallest patch that covers the whole contract while
preserving unrelated behavior. Avoid test edits, generated files, caches, and
logs. Run relevant public tests through Bash using the project's native test command. When the shared command is
available, execute the same native command without replacing
it with an easier test. Inspect the final diff and finish with a non-empty source
patch. Do not use official results, hidden tests, FAIL_TO_PASS ids, grader
patches, or historical outcomes."""


CONTRACT_PROMPT = """\
You are the read-only contract adjudicator for two autonomous coder candidates.
Candidate A was instructed to make the narrowest compatible root-cause repair.
Candidate B was instructed to cover producer, consumer, API, lifecycle, and
edge-case contracts. You cannot edit, merge, or rerun either candidate.

{rules}

Public issue
{goal}

Candidate evidence
{candidates}

Enumerate every explicit behavior requirement in the public issue. For each
requirement, compare the actual A and B diffs against the relevant producer,
consumer, and public API behavior. Cite concrete changed paths and diff details.
Public test records are comparable only when target, runner, and command are
identical. Do not reward larger diffs, stylistic changes, or unsupported claims.
Choose B only when public evidence shows B covers at least one requirement A
does not cover and no requirement is better covered by A. Set
requirements_complete true only after accounting for every explicit issue
requirement. Do not use official outcomes, hidden tests, FAIL_TO_PASS ids,
grader patches, historical results, or model identity."""


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
    return await run_dual_coder(
        ctx, args, selector_prompt=CONTRACT_PROMPT,
        coder_prompts=(MINIMAL_CODER_PROMPT, CROSS_COMPONENT_CODER_PROMPT),
        role_rules=SHARED_RULES,
    )


__all__ = ["validation_council_dual_coder_contract_v1"]
