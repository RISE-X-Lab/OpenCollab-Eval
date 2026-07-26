# OpenCollab compatibility and repository ownership

**English** | [简体中文](MIGRATION.zh-CN.md)

OpenCollab-Eval owns benchmark contracts, evaluator orchestration, Solver
workflows, candidate construction, SWE-bench generation, process isolation,
execution evidence, reporting, and remote evaluation. OpenCollab owns the agent
framework, domain and application services, adapters, composition, compact
public Python API, and framework tests.

## Package ownership

| Owner | Package |
| --- | --- |
| Public and sealed task contracts | `opencollab_eval.contracts` |
| Benchmark normalization | `opencollab_eval.benchmarks` |
| Evaluator and evidence engine | `opencollab_eval.engine` |
| Generation and process isolation | `opencollab_eval.generation` |
| Batch, reporting, and remote commands | `opencollab_eval.commands` |
| Solver workflows | `opencollab_eval.workflows` |
| Shell and configuration assets | `opencollab_eval.resources`, `opencollab_eval.configs` |

The evaluator uses a `src` package layout. Installed commands start modules with
`python -m` or the `oc-eval` console script. Remote execution synchronizes the
declared OpenCollab public package and OpenCollab-Eval runtime, verifies their
tree identity, and then imports from that synchronized package root.

## OpenCollab version boundary

OpenCollab 0.4.0 is the first compatible public API release. The package root
provides `OpenCollab`, `RunResult`, `RunError`, and `workflow`. Optional public
contracts and composition helpers live in `opencollab.environments`,
`opencollab.tools`, and `opencollab.workflows`.

Production code and tests cannot import the retired `opencollab.sdk` namespace
or internal `opencollab.adapters`, `opencollab.application`,
`opencollab.bootstrap`, `opencollab.domain`, and `opencollab.harness`
namespaces. Boundary tests enforce the rule over source and installed wheels.

Evaluation programs, benchmark data, model outputs, predictions, patches,
reports, and integration tests belong to OpenCollab-Eval. Framework behavior
and public API tests belong to OpenCollab.

See [the architecture guide](docs/architecture.md) for the current data flow
and [the wheel contract](CONTRIBUTING.md) for compatibility verification.
