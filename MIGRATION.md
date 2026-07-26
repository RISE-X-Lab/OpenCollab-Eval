# Repository ownership and compatibility plan

OpenCollab-Eval owns benchmark contracts, evaluator orchestration, solver
workflows, SWE-bench generation, process isolation, evidence, reporting, remote
execution, and every test that targets those components. OpenCollab owns the
domain, application services, adapters, bootstrap composition, the compact
public Python API, and their tests.

The migrated implementation uses package namespaces throughout:

| Owner | Package |
| --- | --- |
| Public and sealed task contracts | `opencollab_eval.contracts` |
| Benchmark normalization | `opencollab_eval.benchmarks` |
| Evaluator and evidence engine | `opencollab_eval.engine` |
| Generation and process isolation | `opencollab_eval.generation` |
| Batch, reporting, and remote commands | `opencollab_eval.commands` |
| Solver workflows | `opencollab_eval.workflows` |
| Shell and configuration assets | `opencollab_eval.resources`, `opencollab_eval.configs` |

Top-level `scripts`, `swebench`, and `workflows` Python packages are avoided so
installed packages and repository-root path behavior cannot shadow each other.
Remote execution installs or synchronizes the package under a `src` layout and
starts modules with `python -m`.

The evaluator consumes the compact public API introduced by OpenCollab 0.4.
The package root provides `OpenCollab`, `RunResult`, `RunError`, and `workflow`.
Optional public contracts and composition helpers live in
`opencollab.environments`, `opencollab.tools`, and `opencollab.workflows`.

OpenCollab 0.4.0 is the first compatible release for this boundary. Eval
production code and tests cannot import the retired `opencollab.sdk.*`
namespace or the internal `opencollab.adapters`, `opencollab.application`,
`opencollab.bootstrap`, `opencollab.domain`, and `opencollab.harness`
namespaces. The boundary suite enforces these constraints over both source
trees.
