# eval_adapter

**English** | [简体中文](README.zh-CN.md)

`eval_adapter` translates benchmark rows into the records used by the
evaluation harness. These records retain judge data such as target tests and
reference patches, so the solver input must come from the public task view
prepared by the generation adapter. Failure classification uses explicit
execution evidence and failure scope.

The adapter uses the following models.

| Model | Responsibility |
| --- | --- |
| `TaskSpec` | Normalized task identity, repository, problem statement, base commit, image, tests, and service requirements |
| `WorkspaceSpec` | Image, repository-root candidates, services, and environment required to start a workspace |
| `PatchCandidate` | Solver patch, patch SHA, log paths, token usage, and cost |
| `EvalResult` | Official-evaluation completion, resolved state, technical-failure state, reasons, and log paths |
| `RunRecord` | Final per-task record combining the task, candidate, and evaluation result |

Pro-Lite-specific rules live in `prolite.py`. They cover JSONL dataset loading,
complete Docker image names, `/app`-first repository discovery with `/testbed`
as the fallback, NodeBB Redis requirements, and empty-patch records. Rows with
only an image tag require an explicit image repository.

The technical-failure helper takes failure scope, direct probe outcome, evidence
availability, process quiescence, candidate attribution, and verdict availability.
Unquiesced execution is technical. With quiesced execution, a proven candidate
failure or available verdict determines the semantic result. Missing evidence or
a failed probe with an explicit scope can produce technical reasons. The
official evaluator retains the detailed projection and target-proof checks.

See [the architecture guide](../../../../docs/architecture.md) for the package
boundary and [the evaluation integrity guide](../../../../docs/evaluation-integrity.md)
for candidate, target-proof, and verdict requirements.
