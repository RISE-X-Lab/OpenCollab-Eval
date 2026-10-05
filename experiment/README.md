# Historical research materials

This directory preserves the ICLR experiment inputs and analysis sources from
OpenCollab-Eval commit `0bcc65764d6233d3f7f49a12f987acaff9f1c3b9`. New users should
start with the [current documentation](../docs/README.md). The files here explain
how recorded experiments were configured.

| Directory | Contents |
| --- | --- |
| `batches/` | 337 historical batch specifications and a template, including their original repository commits, slices, retries and replacements |
| `hosts/` | Historical machine descriptions, with a configurable example replacing a private proxy address |
| `suite/` | Task identities, their original order, sampling metadata and recorded replacements |
| `analysis/` | Analysis commands, tests and synthetic examples |
| `history/` | Original source records for interpreting older reports |

Keep these historical inputs together with the results they describe. For a new
run, create a separate experiment directory and worker configuration. The current
batch command accepts `--experiment-dir` and `--host-config`. Its usage is explained
in the [batch guide](batches/README.md); installed analysis modules are described in
[Experiment tools](../src/opencollab_eval/experiment/README.md).

The original collection contains 389 files. Parsed values in the batch
specifications retain the recorded research conditions. Historical source copies
remain under `history/`, including the arm declaration referenced by the installed
observer. Their implementation and host details describe that revision. Current
commands use the installed code and the explicitly selected inputs.

The completed integration's path mapping remains in
[the source coverage record](../docs/iclr-source-coverage.json). Git retains the
original integration sequence. Package compatibility and current contribution
steps are maintained in [MIGRATION.md](../MIGRATION.md) and
[CONTRIBUTING.md](../CONTRIBUTING.md).

Reports distinguish the selected attempt's recorded tokens from recorded tokens
across attempts and excluded replacements. Provider billing and attempts with no
returned usage require the original request records. Official result joins retain
instance, attempt and patch identities. For current result meanings, use
[Evaluation integrity](../docs/evaluation-integrity.md).
