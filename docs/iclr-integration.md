# ICLR evaluation integration series

The target branch is `integrate/iclr-2027`, starting from main `1964568`.
The source boundary is ICLR `0bcc657`. Every series pull request uses this
integration branch as its base and carries its predecessors.

| Order | Head branch | Scope |
| --- | --- | --- |
| 1 | `iclr/01-shared-team` | Public task text, run records, native teams, and public APIs |
| 2 | `iclr/02-scripted` | Scripted collaboration and reading-analyst |
| 3 | `iclr/03-candidates-batch` | Independent candidates and generation batches |
| 4 | `iclr/04-official-outcomes` | SWE-bench 5.0.2 grading and result association |
| 5 | `iclr/05-specification` | Batch specifications and task sampling |
| 6 | `iclr/06-batch-reports` | Batch lifecycle, retries, replacements, and reports |
| 7 | `iclr/07-observers` | Experiment observations and public model inspection |
| 8 | `iclr/08-research-materials` | Research conditions, data, and offline analysis |
| 9 | `iclr/09-complete-history` | Coverage and the verified ICLR merge ancestry |

Use **Create a merge commit** in this order. The final PR merges a separately
verified complete integration with the original ICLR source as its parent.
The source relationship is recorded after every changed source path has a
destination. After accepting this series, the following check succeeds.

```sh
git merge-base --is-ancestor 0bcc657 integrate/iclr-2027
```

The paired runtime is OC's complete verified integration `ebe2c893`.
CI builds that exact runtime through the existing cross-repository setup.
The OC integration series supplies its public team, environment, model
inspection and profile-tool interfaces.

[Source coverage](iclr-source-coverage.json) accounts for all 489 changed source
paths. Base continues to select main's Single2 implementation. Existing
candidate quiescence, extraction, ownership and evidence checks remain active.
Current native-Bash verification and its executed tests replace the source's
module-wide test-skip helper.

All 337 historical batch specifications retain the same parsed YAML data as
the source commit. Two comments were translated to English. The GPU host's
private proxy address is represented by an operator-configured example; the
original configuration remains at the source Git revision. Scanner output and
matching strings retain their values through Unicode literals, with original
Chinese material in the linked historical documents.

Selected-attempt token metrics retain their meaning. Reports expose all
recorded attempts and excluded consumption separately. Result joins preserve
strong attempt identity, and active replacements use the same row selection
for reporting and prediction export. Model queries use the public OC API.

The complete source and series passed 4201 tests with 24 environment-dependent
skips. Every stage also ran imports, command help, related tests and Ruff. The
stable runtime pin passed 70 metadata and public-API tests at every stage.
Final files and Git executable modes match the independently adapted tree
before this source-history merge.
