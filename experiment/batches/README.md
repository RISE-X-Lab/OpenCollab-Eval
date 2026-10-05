# Batch specifications and the current batch command

The YAML files in this directory record historical experiments. Their host names,
model files, repository commits and task slices belong to those runs. Create a
separate specification and worker file for a new experiment, and retain the
original files with their existing results.

For a first benchmark run, use [Getting started](../../docs/getting-started.md) or
the [Duo and SWE Pro-Lite guide](../../docs/swe-prolite-operations.md). This page is
for operators using the specification-driven research batch commands.

## Read the command help

Global options go before the action. Each action provides its own help.

```bash
python -m opencollab_eval.commands.batch --help
python -m opencollab_eval.commands.batch plan --help
python -m opencollab_eval.commands.batch launch --help
python -m opencollab_eval.commands.batch score --help
python -m opencollab_eval.commands.batch report --help
```

## Plan a new batch

The experiment directory holds the suite and host inputs referenced by the spec.
The explicit worker file overrides the historical host file named in that spec.
Place credentials in the worker's private environment file.

```bash
python -m opencollab_eval.commands.batch \
  --experiment-dir /path/to/experiment \
  --host-config /path/to/worker.yaml \
  plan /path/to/experiment/batches/new-run.yaml
```

`plan` prepares local instance data and launch information. Review the selected
instances, model settings, repository commits and output directory before running
`preflight` against the worker. The launcher retains its existing runtime,
candidate and output-ownership checks.

| Action | What it does |
| --- | --- |
| `plan` | Prepare the local batch plan from the spec and its task inputs |
| `preflight` | Check the configured remote worker and batch inputs |
| `launch` | Start or resume the declared batch on the worker |
| `status` | Read the recorded batch and driver state |
| `wait` | Wait for the recorded batch to finish |
| `pull` | Copy the batch's outputs into local result storage |
| `score` | Run the configured official evaluator for saved candidates |
| `score-report` | Read official scoring reports and their controls |
| `report` | Report the selected generation attempts and recorded usage |

## Resume and recover

Keep the same spec and output ownership for a resume. Use a new batch identity for
a new experiment. A retry names its parent through `retry_of`; a task replacement
uses `replaces`. The installed command checks the related batch's actual model and
configuration before treating the results as part of the same experiment.

An SSH timeout or exit code 255 leaves the remote action's outcome unknown. Check
`status`, the recorded launch and the actual driver before starting another action.
Read-only probes may retry their transport; the launch operation has separate
ownership and reconciliation logic. Keep partial outputs and logs available for
recovery.

A generated prediction and an official passing result are different stages. Read
the candidate's failure reason and the official test report before classifying it.
A host preparation error requires environment repair and a retry of the same saved
candidate when it is available. A normal failing assertion remains a test failure.
See [Troubleshooting](../../docs/troubleshooting.md) for the current recovery paths.

## Check a driver manually

To verify that the manual check sees a planted process, start `bash -c 'exec -a "python -m opencollab_eval.generation.gen_prediction_batch --out-dir positive-control" sleep 30' &` and note its PID. This temporary process exits after 30 seconds.

Inspect `ps -eo pid,etime,args | grep '[o]pencollab_eval[.]generation[.]gen_prediction_batch'` and confirm that the planted PID appears before interpreting the real driver list. Read the actual matching command lines and match the intended output directory. An empty result from a check that missed its control leaves the driver state unknown. The bracketed pattern uses regular-expression matching. Fixed-string grep would change its meaning.

## Read the outputs

`batch.json` records the batch inputs and launch information. Prediction files hold
saved candidates. `metrics.jsonl` and per-instance logs describe generation.
Official evaluation produces its own reports, which `score-report` joins to the
selected attempts.

Retries, replacements and withdrawals remain in the recorded history. Reports show
selected-attempt usage, usage across attempts and excluded consumption separately.
Missing usage remains unknown. The implementation is described in
[Experiment tools](../../src/opencollab_eval/experiment/README.md).

The task order and historical replacement example are documented in
[the suite record](../suite/README.md). Full historical instructions remain in the
repository's Git history; their machine-specific commands have been removed from
this current guide.
