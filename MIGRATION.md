# OpenCollab compatibility and experiment ownership

**English** | [简体中文](MIGRATION.zh-CN.md)

OpenCollab-Eval owns benchmark normalization, evaluator execution, candidate
construction and scoring evidence. Candidate construction runs under process
isolation, and its outputs retain execution evidence.
OpenCollab owns the agent framework, its public Python API, and framework tests.

Experiment commands and workflows belong to
[OpenCollabExp](https://github.com/KaiEureka/OpenCollabExp).
Use `oc-exp g22`, `oc-exp swe-run`, `oc-exp rejudge-queue`, `oc-exp package-runtime`
and `oc-exp final-report` for that layer. OCE retains `inspect`, `run` and `score`.
Workflows are explicit `module:function` inputs. The proposed interfaces live on
`codex/extract-experiment-layer`, with the main branch retained.

## Package ownership

| Owner | Package |
| --- | --- |
| Public and sealed task contracts | `opencollab_eval.contracts` |
| Benchmark normalization | `opencollab_eval.benchmarks` |
| Evaluator and evidence engine | `opencollab_eval.engine` |
| Generation and process isolation | `opencollab_eval.generation` |
| Core CLI and scoring entry points | `opencollab_eval.commands` |
| Evaluation process resources | `opencollab_eval.resources` |
| Experiment workflow methods | `opencollab_exp.workflows` |
| Campaign, deployment and presentation | `opencollab_exp.commands` |

The evaluator uses a `src` package layout. Installed commands start modules with
`python -m` or the `oc-eval` console script. Remote execution synchronizes the
declared OpenCollab public package and OpenCollab-Eval runtime, verifies their
tree identity, and then imports from that synchronized package root.

## OpenCollab version boundary

OpenCollab-Eval 0.7.0 requires OpenCollab 0.7.x. The historical 0.5.1 release
paired with OpenCollab 0.5.0, as recorded in the changelog. The current public API provides
the Responses transport, runtime identity checks, and public test contracts
used by the current evaluator. The package root provides `OpenCollab`,
`RunResult`, `RunError`, and `workflow`. Optional public contracts and
composition helpers live in `opencollab.environments`, `opencollab.tools`, and
`opencollab.workflows`.

Production code and tests cannot import the retired `opencollab.sdk` namespace
or internal `opencollab.adapters`, `opencollab.application`,
`opencollab.bootstrap`, `opencollab.domain`, and `opencollab.harness`
namespaces. Boundary tests enforce the rule over source and installed wheels.

Reusable evaluation mechanisms and their tests belong to OpenCollab-Eval.
Framework behavior and public API tests belong to OpenCollab. Benchmark data,
model outputs, predictions, patches and reports belong to the caller's experiment
workspace. Deployment operations are documented in
[OpenCollabExp's SWE Pro-Lite guide](https://github.com/KaiEureka/OpenCollabExp/blob/main/docs/swe-prolite-operations.md).

See [the architecture guide](docs/architecture.md) for the current data flow
and [the wheel contract](CONTRIBUTING.md) for compatibility verification.
