# OpenCollab-Eval documentation

**English** | [简体中文](zh-CN/README.md)

Choose a guide by the task you want to complete. The repository [README](../README.md) introduces version 0.9.1 and the main commands. [Getting started](getting-started.md) walks through installation, task input, a first run, and the result file.

## Prepare and run tasks

| What you need | Guide |
| --- | --- |
| Install and run a local repository task | [Getting started](getting-started.md) |
| Prepare the correct JSONL input | [Task and dataset formats](task-formats.md) |
| Run Duo on one Linux worker, then expand to a batch | [SWE Pro-Lite operations](swe-prolite-operations.md) |
| Run server queues and manage provider capacity | [Evaluation suite](evaluation-suite.md) |
| Find installed commands and options | [CLI reference](cli-reference.md) |
| Configure official scoring for another task family | [Scoring adapters](scoring-adapters.md) |

## Read results and recover a run

| What you need | Guide |
| --- | --- |
| Interpret candidate eligibility and official outcomes | [Evaluation integrity](evaluation-integrity.md) |
| Investigate an error or stalled run | [Troubleshooting](troubleshooting.md) |
| Understand runtime stages and evidence files | [Evaluation runtime](evaluation-runtime.md) |
| Produce a comparison with `oc-eval final-report` | [Final report guide](final-report.md) |

## Understand and develop the evaluator

| What you need | Guide |
| --- | --- |
| Follow the package structure and OC dependency | [Architecture](architecture.md) |
| Understand foreground commands and container candidate adoption | [Terminal container runtime](terminal-container-runtime.md) |
| Prepare task prerequisites and original scoring budgets | [Terminal verifier preparation](terminal-verifier-preparation.md) |
| Read Duo candidate evidence and the selector's tools | [Duo file evidence](duo-file-evidence.md) |
| Understand controller-owned candidate extraction | [Trusted candidate construction](design/trusted-candidate-construction.md) |
| Run the installed-wheel SSH and Docker test | [Deterministic SWE E2E](testing/deterministic-swe-e2e.md) |

[MIGRATION.md](../MIGRATION.md) explains OC and OCE ownership. [CONTRIBUTING.md](../CONTRIBUTING.md) covers development and review, and [SECURITY.md](../SECURITY.md) explains private security reporting.

The [integrity coverage ledger](integrity-coverage.json) maps scoring requirements to implementation and regression tests. Historical research sources remain in the [experiment index](../experiment/README.md) and [ICLR source coverage](iclr-source-coverage.json).
