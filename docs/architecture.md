# OpenCollab-Eval Architecture

**English** | [简体中文](zh-CN/architecture.md)

The dependency direction is `opencollab_exp -> opencollab_eval -> opencollab`.
Pier is an independent backend invoked by the experiment layer.

## Evaluation responsibilities

The evaluator owns task data, resource lifecycle, solver invocation, candidate
construction, independent scoring, and structured result evidence. The `engine`
package includes reusable lifecycle mechanisms and benchmark-specific SWE
adapters. The `generation` package supplies solver integration and candidate
collection. `contracts`, `benchmarks`, and `verification` define task boundaries
and interpret observed execution evidence.

## Method injection

Callers supply a workflow as `module:function`. `workflow_loader` imports that
explicit entry point. Method registries, role prompts, method budgets, and
friendly names belong to the caller. Candidate preparation is separately
selected as `shared`, `isolated`, or `lean`. The existing hidden-test isolation
and candidate integrity behavior stays in the evaluator.

## Entry points

`inspect` normalizes benchmark input. `run` invokes the task lifecycle and
reports candidate submission eligibility. `score` uses the existing upstream
SWE-bench harness wrapper. The Pro-Lite direct evaluator remains available as a
library execution surface for callers that supply its configuration. It keeps
the dataset F2P/P2P targets, original scoring resources, and evidence rules.

## Runtime ownership

A caller supplies the generation launcher path, workflow reference, method label,
and declared method environment keys. The evaluator retains ownership checks
for containers, processes, candidates, and scores. Deployment credentials,
provider pools, campaign scheduling, and remote synchronization belong to
OpenCollabExp. Evaluator installation and tests work without the experiment
package.

## Existing compatibility

The Pro-Lite runner still composes its existing evaluator modules through its
compatibility namespace. That mechanism remains within OCE for this migration.
Experiment-specific presets and commands are separated through explicit inputs.
