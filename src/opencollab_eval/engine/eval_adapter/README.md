# eval_adapter

**English** | [简体中文](README.zh-CN.md)

`eval_adapter` translates benchmark rows into the records used by the
evaluation harness. It also maps infrastructure failures from official
evaluation to stable fields.

The adapter uses the following models.

| Model | Responsibility |
| --- | --- |
| `TaskSpec` | Normalized task identity, repository, problem statement, base commit, image, tests, and service requirements |
| `WorkspaceSpec` | Image, repository-root candidates, services, and environment required to start a workspace |
| `PatchCandidate` | Solver patch, patch SHA, log paths, token usage, and cost |
| `EvalResult` | Official-evaluation completion, resolved state, technical-failure state, reasons, and log paths |
| `RunRecord` | Final per-task record combining the task, candidate, and evaluation result |

Pro-Lite-specific rules live in `prolite.py`. They cover JSONL dataset loading,
Docker image names, `/app`-first repository discovery, NodeBB Redis requirements,
empty-patch records, and technical-failure classification for Redis, SSH, Docker,
timeouts, test-patch application, and missing reports.

See [the architecture guide](../../../../docs/architecture.md) for the package
boundary and [the evaluation integrity guide](../../../../docs/evaluation-integrity.md)
for candidate, target-proof, and verdict requirements.
