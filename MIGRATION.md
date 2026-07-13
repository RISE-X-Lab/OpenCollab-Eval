# Migration plan

The source repository remains the recovery authority until every migrated group
passes equivalence tests. The target package never creates top-level `swebench`,
`scripts`, or `workflows` packages because those names collide with installed
packages and depend on repository-root `sys.path` behavior.

| Source group | Target owner |
| --- | --- |
| `opencollab.harness.eval_adapter` | `contracts`, `benchmarks.swe_batch_pro` |
| `opencollab.harness.solver_backend` | `contracts.solvers` |
| `opencollab.harness.workflow_backend` | `solvers.opencollab` |
| `opencollab.harness.swe_eval_*` | `evidence` |
| `opencollab.harness.swe_generation_proof` | `evidence.generation_proof` |
| `opencollab.harness.evaluator*` | `orchestration` |
| `opencollab.harness.swe_checkpoint*` | `orchestration.checkpoints` |
| `opencollab.harness.swe_v1_remote_*` | `remote` |
| repository `swebench/` generation modules | `generation`, `isolation`, `solvers` |
| evaluation scripts | `cli`, `remote`, `reporting`, `official_eval` |
| solver workflows | `workflows` |

The next migration group is evidence and record handling. Container snapshot,
quiescence, trusted patch extraction, and their proof schema then move together.
Official evaluation follows after patch identity and execution evidence are
stable. Remote execution moves last and installs hashed OpenCollab and
OpenCollab-Eval wheels instead of copying source directories.

