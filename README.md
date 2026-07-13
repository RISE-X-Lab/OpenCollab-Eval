# OpenCollab-Eval

OpenCollab-Eval owns benchmark adaptation, solver isolation, trusted patch
extraction, official evaluation, evidence, batch orchestration, and reporting for
OpenCollab-based software-engineering experiments.

This prototype targets OpenCollab 0.2 and the `experiment/openhands-eval-layer`
branch. It proves the first repository boundary: evaluation code imports
OpenCollab only through `opencollab.sdk`.

The initial vertical slice includes normalized public and sealed task models, a
SWE-Batch Pro adapter, an OpenCollab Workflow solver, the `base-team` workflow,
an inspection CLI, and dependency-boundary tests. The Workflow solver reports
runtime completion and accounting only. Trusted patch extraction and official
verdicts remain separate evaluation responsibilities.

```bash
oc-eval inspect path/to/tasks.jsonl --identity-key-file path/to/sealed-identity.key
```

See [MIGRATION.md](MIGRATION.md) for the remaining extraction map.

Release compatibility is verified from built artifacts rather than editable
source trees. Build both wheels, then run
`scripts/verify_wheel_contract.sh PATH_TO_OC_WHEEL PATH_TO_EVAL_WHEEL`; the
script installs both distributions in a fresh virtual environment, runs the
Eval tests against the installed OpenCollab package, and checks the packaged
CLI.

The identity key is an evaluator-owned file containing exactly 32 random bytes.
Keep it in sealed run state and reuse it for retries of the same batch so public
task IDs remain stable without exposing benchmark instance identifiers.
