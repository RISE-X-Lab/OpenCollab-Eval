# OpenCollab-Eval

OpenCollab-Eval owns benchmark adaptation, solver isolation, trusted patch
extraction, official evaluation, evidence, batch orchestration, and reporting for
OpenCollab-based software-engineering experiments.

This prototype was extracted from OpenCollab commit
`4b14504` on `experiment/openhands-eval-layer`. It proves the first repository
boundary: evaluation code imports OpenCollab only through `opencollab.sdk`.

The initial vertical slice includes normalized public and sealed task models, a
SWE-Batch Pro adapter, an OpenCollab Workflow solver, the `base-team` workflow,
an inspection CLI, and dependency-boundary tests. The Workflow solver reports
runtime completion and accounting only. Trusted patch extraction and official
verdicts remain separate evaluation responsibilities.

```bash
oc-eval inspect path/to/tasks.jsonl
```

See [MIGRATION.md](MIGRATION.md) for the remaining extraction map.

