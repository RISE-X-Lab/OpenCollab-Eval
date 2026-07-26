# Evaluation workflows

This package contains deterministic multi-agent workflows for evaluation.
Ordinary Python controls agent fan-out, repair rounds, verification gates, and
stop conditions. The model handles repository reasoning and edits within those
fixed control-flow boundaries.

The package depends only on the OpenCollab 0.4 workflow-authoring surface:

```python
from opencollab.tools import Tool, builtin_tools
from opencollab.workflows import WorkflowContext, workflow
```

It does not import OpenCollab adapters, application services, bootstrap
internals, domain modules, harness code, or the retired `opencollab.sdk`
submodules.

## Bundled workflows

| Name | Structure |
| --- | --- |
| `base-team` | Analyst brief followed by a bounded coder and tester loop |
| `self-collab` | Sequential phases with plan review and per-phase verification |
| `split-solve` | Independent subtasks followed by combined verification |
| `scout-solve` | Parallel read-only reconnaissance followed by one repair loop |
| `analyst-solve` | Analyst-led reconnaissance, phased repair, and final verification |
| `team-pro` | Stable tuned alias for `analyst-solve` |
| `validation-council-solve` | Blind contract and validation council for SWE tasks |
| `swe-committee-v2` | Committee workflow with explicit evidence and test gates |

Blind SWE workflows receive issue text, repository contents, public tests, and
public documentation. They do not receive hidden grader assertions. Final task
resolution remains the responsibility of the external official evaluation.

## Authoring contract

A workflow is an async function decorated with `@workflow`. Role tools come
from `builtin_tools`, which returns fresh headless-safe instances and disables
model-supplied test command overrides.

```python
from typing import Any

from opencollab.tools import builtin_tools
from opencollab.workflows import WorkflowContext, workflow


@workflow(
    name="my-flow",
    description="Inspect a task and report evidence.",
    phases=["inspect"],
)
async def my_flow(
    ctx: WorkflowContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal"'}

    await ctx.phase("inspect")
    report = await ctx.agent(
        f"Inspect the repository and report evidence.\n\nGoal:\n{goal}",
        label="analyst",
        tools=builtin_tools("bash", "file_read", "grep"),
    )
    return {
        "status": "done" if report else "incomplete",
        "report": report,
        "tokens_spent": ctx.tokens_spent(),
    }
```

`WorkflowContext.agent` runs one bounded session. A schema requests validated
structured output. `parallel` executes zero-argument async thunks under the
runtime concurrency limit. `phase` and `log` publish progress. `source_changed`
and `diff` inspect the current work tree when the runtime supplies a probe.
`tokens_spent`, `tokens_remaining`, `seconds_left`, and `time_low` expose
read-only run budgets.

Tool lists should match the role. Read-only roles normally use `file_read` and
`grep`, with `bash` only when an executable probe is required. Coders may add
`file_write` and `apply_patch`. Test gates require `run_tests`. Diff auditors
use `git_diff`. Passing `allow_file_creation=False` to `builtin_tools` prevents
`file_write` from creating new files.

Every `run_tests` instance created through the public helper rejects runner
overrides and extra model-supplied arguments. A workflow may inspect the
instance's parser-backed `verified_targets` after the call when a benchmark
requires exact target execution evidence. A model-written `tests_run` field
does not replace executable evidence.

## Conventions

Each `ctx.agent` result can be `None`, so every stage supplies an explicit
fallback or reports an incomplete result. Structured results are checked with
`isinstance(result, dict)`. Repair loops include previous verifier findings and
have fixed round limits. Gate roles retain an executable probe before issuing
`PASS`. Workflow return values use `done`, `incomplete`, or `error` and include
`ctx.tokens_spent()`.

Shared helpers live in private modules whose names start with an underscore.
Public workflow functions are exported from this package's `__init__.py`.

## Running

```bash
python -m opencollab_eval.generation.gen_prediction_workflow \
  --instance-file /path/to/instance.json \
  --output /path/to/predictions.jsonl \
  --workflow validation-council-solve
```

The generator owns selection and execution of bundled workflows. Installed
consumers import workflow functions from `opencollab_eval.workflows`; they do
not depend on OpenCollab's internal workflow discovery implementation.
