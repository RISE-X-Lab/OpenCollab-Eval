# Repository ownership and compatibility plan

OpenCollab-Eval owns benchmark contracts, evaluator orchestration, solver
workflows, SWE-bench generation, process isolation, evidence, reporting, remote
execution, and every test that targets those components. OpenCollab owns the
domain, application services, adapters, bootstrap composition, the stable SDK,
and their tests.

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

The evaluator consumes focused public modules under `opencollab.sdk`: agents,
configuration, environments, files, lifecycle, models, persistence, repository,
retirement, runtime, tools, tracing, usage, and workflows. OpenCollab 0.3.0 is
the first compatible release for this boundary. Eval production code and tests
cannot import compatibility shims, experimental APIs, private SDK names, or OC
adapters, application, bootstrap, domain, and retired harness modules. The
boundary suite enforces these constraints over both source trees.
