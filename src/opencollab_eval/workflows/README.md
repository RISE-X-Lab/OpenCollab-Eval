# Evaluation workflows

Deterministic multi-agent **workflows** — plain Python that orchestrates
one-shot agent sessions. Unlike a `team` (where an LLM *lead* decides who to
spawn via `spawn_agent`), a workflow's control flow is ordinary Python: loops,
round caps, never-identical retries, parallel fan-out, and stop conditions are
*guaranteed* by code, not prompted.

Each module here is an evaluation workflow built only on the versioned
`opencollab.sdk` interface. Public workflow functions are exported from the
package `__init__.py`.

## Bundled workflows

| Name | Shape | Front half | Solve loop |
|------|-------|-----------|-----------|
| `base-team` | compact specialist sequence | analyst produces a structured brief | coder/tester loop with at most two repair rounds |
| `self-collab` | sequential phases | analyze → parallel plan review (2 lenses, one revision) | per-phase coder/tester, **stops** on first failed phase |
| `split-solve` | independent subtasks | analyze → split into disjoint subtasks | per-subtask coder/tester (failures don't block others) → synthesize |
| `scout-solve` | parallel reconnaissance | analyze into dimensions → parallel read-only scouts → synthesize a brief | single coder/tester loop from the brief |
| `analyst-solve` | analyst-led phased repair | parallel read-only reconnaissance → analyst-owned phased plan | best-effort coder/tester phases → final verify with a budget floor |
| `team-pro` | tuned stable alias | same analyst-led reconnaissance as `analyst-solve` | same phased implementation and verification contract |
| `validation-council-solve` | blind validation council | localize → contract miner + test cartographer → validation judge + baseline triage | coder → patch validator → diff risk auditor → post-patch validation judge → final verifier, with one minimal retry |
| `swe-committee-v2` | committee topology | localize → contract miner + test cartographer + observable inventory → tribunal + pre-validate | coder → existing tests + approved validation → diff-risk stage (branch/boundary/impact/diff review) → post-patch judge + triage → final skeptic → final verifier, with up to two minimal retries |

Pick the front half by how the *uncertainty* is shaped: one linear path
(`self-collab`), several disjoint fixes (`split-solve`), or one fix that first
needs broad understanding (`scout-solve` or `analyst-solve`).

For SWE-bench-style blind evaluation, use `validation-council-solve` or
`swe-committee-v2`. The packaged
`python -m opencollab_eval.generation.gen_prediction_workflow --workflow
<workflow>` command defaults to blind validation for these modes, so it
withholds official `test_patch` and `FAIL_TO_PASS` ids. The workflow must infer
validation from the issue, repository code, public tests, and public docs. For
these workflows, the SWE prediction JSONL uses the guarded staged diff produced
by the generator; the generic `run_eval_task` patch remains a workflow
observability field.

## How discovery works

`discover_workflows(path)` from `opencollab.sdk` can load a directory of
workflow modules and register values carrying a `__workflow_spec__` attribute.

- Files starting with `_` (and dunder names like `__init__.py`) are **skipped** —
  use a leading underscore for shared helper modules you don't want registered.
- The registry **rejects duplicate names**, so each `name=` must be unique.
- Packaged callers obtain the bundled directory with
  `importlib.resources.files("opencollab_eval.workflows")`; the generator and
  solver adapter already do this.
- A missing directory yields an empty registry (no error).

## Writing a new workflow

A workflow is a single async function `async def fn(ctx, args) -> Any` tagged
with `@workflow`. Minimal skeleton:

```python
"""my-flow — one-line summary of the topology."""
from __future__ import annotations

from typing import Any

from opencollab.sdk.tools import BashTool, FileReadTool, GrepTool
from opencollab.sdk.workflows import workflow


@workflow(
    name="my-flow",                       # unique; this is the CLI name
    description="What it does, in one line.",
    phases=["analyze", "solve"],          # optional; labels for progress display
)
async def my_flow(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    # `goal` for CLI runs; `description` is what the eval harness passes.
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal"'}

    await ctx.phase("analyze")
    findings = await ctx.agent(
        f"Investigate, read-only, then report:\n{goal}",
        label="analyst",
        tools=[BashTool(), FileReadTool(), GrepTool()],
    )

    await ctx.phase("solve")
    # ... drive a coder/tester loop, fan out, synthesize, etc.
    return {"status": "done", "findings": findings, "tokens_spent": ctx.budget.spent()}
```

Save new bundled modules under `src/opencollab_eval/workflows/`, export their
public workflow function from the package `__init__.py`, and add focused tests.
The packaged prediction generator can then select the registered name through
`--workflow`.

### The `ctx` primitives (`opencollab.sdk.WorkflowContext`)

| Primitive | Signature | Returns |
|-----------|-----------|---------|
| `ctx.agent` | `agent(prompt, *, schema=None, label=None, tools=None, isolation=False)` | the session's final text (`str`), or the validated `dict` when `schema=` is given, or `None` if the agent died |
| `ctx.parallel` | `parallel(thunks)` — `thunks` are zero-arg callables returning awaitables | `list` in input order; a thunk that raises → `None` in its slot |
| `ctx.pipeline` | `pipeline(items, *stages)` — each `stage(prev, item, idx)`; **no** inter-stage barrier | `list` in input order; a stage that raises drops that item to `None` |
| `ctx.phase` / `ctx.log` | `phase(title)` / `log(message)` | observability; printed by the CLI as `== title` / `-- message` (no-op when nothing is wired) |
| `ctx.budget` | `.total` (`int \| None`), `.spent()`, `.remaining()` | live token accounting across every session created so far |

**Concurrency** is bounded by a shared semaphore (CLI `--concurrency`, default 4);
`parallel`/`pipeline` may pass more items than that — the excess queues.

**Structured output:** pass `schema=<JSON Schema dict>`. The engine injects a
`structured_output` tool, instructs the agent to finish by calling it, validates
the payload, and returns the dict (one corrective retry on the same session,
then `None`). Always guard: `if not isinstance(result, dict): ...`.

**Parallel fan-out** — bind loop variables with default args so the late-binding
closure bug doesn't make every thunk see the last item:

```python
reports = await ctx.parallel(
    [
        (lambda d=d, i=i: ctx.agent(PROMPT.format(**d), label=f"scout:{i}", tools=_read_tools()))
        for i, d in enumerate(dimensions)
    ]
)
```

### Failure contract (important)

Every primitive localizes failure: a dead agent, a raising thunk, or a raising
pipeline stage yields `None` for *that unit of work only* and **never aborts the
fleet**. The single exception that escapes is `WorkflowBudgetExceeded`, raised by
`ctx.agent` only when the budget is *already* exhausted before a call starts. So:

- check `if result is None` / `if not isinstance(result, dict)` after every
  `ctx.agent` and substitute a sensible fallback;
- treat partial results from `parallel`/`pipeline` as normal (`.filter`/skip the
  `None`s, and `log` the ratio so a silent loss is visible).

### Tools (`opencollab.sdk`)

Provision each agent with exactly the tools its role needs (least privilege):

| Tool | Module | Role typically using it |
|------|--------|------------------------|
| `BashTool` | `tools.bash` | everyone (escape hatch) |
| `FileReadTool`, `GrepTool` | `tools.fs` | read-only: analyst / reviewer / scout / tester |
| `FileWriteTool` | `tools.fs` | coder / synthesizer |
| `ApplyPatchTool` | `tools.apply_patch` | coder (fallback edit) |
| `RunTestsTool` | `tools.run_tests` | coder / tester |

The bundled workflows factor these into `_read_tools()`, `_coder_tools()`,
`_tester_tools()` — copy that pattern.

### Conventions worth reusing

The bundled workflows share idioms that earned their keep; lift them rather
than reinventing:

- **`SHARED_RULES`** — a tool-discipline + smallest-correct-change block handed to
  every role.
- **`VERDICT_SCHEMA`** with `PASS` / `FAIL` / `BLOCKED` — `BLOCKED` lets a tester
  flag an *environmental* dead end (missing dep, no network) so the loop stops
  instead of burning rounds.
- **Never re-issue an identical task** — carry the tester's `findings` into the
  next coder round (`FINDINGS_BLOCK`), and cap rounds (`MAX_*_ROUNDS`).
- **`goal` / `description` fallback** so the eval harness can run the workflow
  unchanged.
- **Return `tokens_spent: ctx.budget.spent()`** and a `status` of
  `done` / `incomplete` / `error` for uniform downstream handling.

## Running

```bash
python -m opencollab_eval.generation.gen_prediction_workflow \
  --instance-file /path/to/instance.json \
  --output /path/to/predictions.jsonl \
  --workflow scout-solve
```

Installed integrations can discover the same bundled workflows without relying
on a source checkout:

```python
from importlib.resources import files

from opencollab.sdk.workflows import discover_workflows

workflow_dir = files("opencollab_eval.workflows")
registry = discover_workflows(str(workflow_dir))
spec = registry.get("scout-solve")
```

Run the generator with `--help` for model, budget, timeout, container-image,
and output options. The final result is appended to the prediction and metrics
JSONL files supplied by the caller.

## Architecture boundary

Workflow modules import their decorator, context contract, and tools from the
versioned `opencollab.sdk` package. Evaluation engines, benchmark adapters,
generation commands, and tests remain in `opencollab_eval`. Keep new workflows
declarative: orchestration logic belongs here and runtime capabilities remain
behind SDK tools.
