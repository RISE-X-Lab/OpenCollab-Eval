# OpenCollab compatibility and repository ownership

**English** | [简体中文](MIGRATION.zh-CN.md)

OpenCollab-Eval owns the benchmark and evaluation code. This covers Solver
workflows and remote evaluation. Candidate construction runs under process
isolation, and its outputs retain execution evidence.
OpenCollab owns the agent framework, its public Python API, and framework tests.

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

OpenCollab-Eval 0.5.0 requires OpenCollab 0.5.0. This paired release provides
the Responses transport, runtime identity checks, and public test contracts
used by the current evaluator. The package root provides `OpenCollab`,
`RunResult`, `RunError`, and `workflow`. Optional public contracts and
composition helpers live in `opencollab.environments`, `opencollab.tools`, and
`opencollab.workflows`.

Production code and tests cannot import the retired `opencollab.sdk` namespace
or internal `opencollab.adapters`, `opencollab.application`,
`opencollab.bootstrap`, `opencollab.domain`, and `opencollab.harness`
namespaces. Boundary tests enforce the rule over source and installed wheels.

Evaluation programs, benchmark data, model outputs, predictions, patches,
reports, and integration tests belong to OpenCollab-Eval. Framework behavior
and public API tests belong to OpenCollab.

See [the architecture guide](docs/architecture.md) for the current data flow
and [the wheel contract](CONTRIBUTING.md) for compatibility verification.
