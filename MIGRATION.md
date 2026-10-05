# Compatibility and upgrading

**English** | [简体中文](MIGRATION.zh-CN.md)

OpenCollab runs the agents. OpenCollab-Eval prepares benchmark tasks, preserves their generated patches, runs the official tests, and reports the results. Use this page when choosing matching versions or updating an existing installation.

## Package responsibilities

| Area | Owner |
| --- | --- |
| Agent sessions and the built-in Duo workflow | OpenCollab public API |
| Public tasks and private judge inputs | `opencollab_eval.contracts` |
| Benchmark input conversion | `opencollab_eval.benchmarks` |
| Candidate generation and process isolation | `opencollab_eval.generation` |
| Official evaluation and result evidence | `opencollab_eval.engine` |
| Batch commands and reports | `opencollab_eval.commands` |
| Evaluator-owned research workflows | `opencollab_eval.workflows` |
| Bundled shell scripts and configuration | `opencollab_eval.resources`, `opencollab_eval.configs` |

The evaluator uses a `src` package layout. Installed entry points are `oc-eval` and `python -m opencollab_eval`. Runtime preparation packages or synchronizes the declared framework and evaluator sources before a worker imports them.

## Choose compatible versions

The current evaluator release is 0.9.1. Its dependency is `opencollab>=0.9,<0.10`, and CI tests it with OpenCollab 0.9.1. The exact tested framework commit is recorded in `.github/workflows/ci.yml`.

Follow [Getting started](docs/getting-started.md) for release-wheel or source installation. Confirm what the active environment actually imports before starting a run.

```bash
oc-eval --version
python -c 'from importlib.metadata import version; print("OC", version("opencollab")); print("OCE", version("opencollab-eval"))'
```

The public framework entry points include `OpenCollab`, `RunResult`, `RunError`, and `workflow`. Evaluator integrations also use the documented `opencollab.builtin_workflows`, `opencollab.environments`, `opencollab.patches`, `opencollab.profiles`, `opencollab.models`, `opencollab.teams`, `opencollab.tools`, and `opencollab.workflows` modules. Internal framework imports and the retired `opencollab.sdk` namespace are rejected by the existing source and installed-wheel checks.

## Keep existing runs interpretable

Create a separate environment for an upgrade and retain the original configuration and outputs of completed runs. A package upgrade changes the runtime for future generation. Reusing a saved candidate for scoring follows the candidate and test identity checks described in [Evaluation integrity](docs/evaluation-integrity.md).

Historical release changes remain in [CHANGELOG.md](CHANGELOG.md). The completed research integration is recorded in [source coverage](docs/iclr-source-coverage.json) and [historical experiment materials](experiment/README.md). Current development and release checks are in [CONTRIBUTING.md](CONTRIBUTING.md) and [RELEASING.md](RELEASING.md).
