# OpenCollab-Eval documentation

**English** | [简体中文](zh-CN/README.md)

This directory separates operator guidance, contracts, architecture, and design
records so that an implementation plan is never mistaken for a command guide.

## Start here

| Reader goal | Document |
| --- | --- |
| Install the package and run the first local command | [Getting started](getting-started.md) |
| Prepare dataset or generic task JSONL | [Task formats](task-formats.md) |
| Run a real remote SWE Pro-Lite task | [SWE Pro-Lite operations](swe-prolite-operations.md) |
| Choose the correct command | [CLI reference](cli-reference.md) |
| Understand components and dependencies | [Architecture](architecture.md) |
| Understand trusted results and failure states | [Evaluation integrity](evaluation-integrity.md) |
| Diagnose a failed run | [Troubleshooting](troubleshooting.md) |
| Publish a validated 100-task comparison | [Final report contract](final-report.md) |

The repository-level [README](../README.md) gives the shortest complete
overview. [MIGRATION.md](../MIGRATION.md) defines ownership between OpenCollab
and OpenCollab-Eval. [CONTRIBUTING.md](../CONTRIBUTING.md) describes development
and review requirements. [SECURITY.md](../SECURITY.md) contains the private
reporting process.

## Operator and contract documents

[evaluation-runtime.md](evaluation-runtime.md) maps installed commands to
runtime layers and explains which entrypoints produce candidates or official
verdicts. [final-report.md](final-report.md) is the complete input and evidence
contract for `oc-eval final-report`.

The machine-readable [integrity coverage ledger](integrity-coverage.json) maps
known integrity requirements to owners, implementation files, tests, and exact
test node IDs. It is verified by the test suite and should be updated with the
corresponding implementation and regression test.

## Design and verification records

[Trusted candidate construction](design/trusted-candidate-construction.md)
describes the implemented controller-owned Git projection. [Deterministic SWE
E2E](testing/deterministic-swe-e2e.md) describes the installed-wheel test that
uses ephemeral SSH, a fake model service, Docker, candidate extraction, and
official target execution.

Design records explain why the implementation has its current shape. Testing
records describe executable verification. Operators should use the
getting-started, Pro-Lite, CLI, and troubleshooting guides for commands.
