# CLI reference

**English** | [简体中文](zh-CN/cli-reference.md)

The installed commands cover common operations. Advanced module entrypoints
are available for repository operators and tests. Run `--help` on the installed
revision for its complete option list.

## Installed command

Both forms below invoke the same package.

```bash
oc-eval --help
python -m opencollab_eval --help
```

### `oc-eval inspect`

```text
oc-eval inspect DATASET --identity-key-file KEY
                       [--image-repository REPOSITORY]
```

This command validates a bounded SWE-Batch Pro JSONL file, separates public and
sealed fields, and prints anonymous public task IDs. Processing stops after
inspection, before generation and official evaluation.

### `oc-eval run`

```text
oc-eval run TASKS_FILE --model MODEL --provider PROVIDER
            [--api-key KEY] [--base-url URL] [--output DIRECTORY]
            [--concurrency COUNT] [--max-tokens COUNT] [--timeout SECONDS]
            [--temperature VALUE] [--top-p VALUE] [--agent-profile {single,single2}]
            [--no-progress-timeout SECONDS] [--generation-wall-timeout SECONDS]
```

This command runs the generic evaluator and writes `results.jsonl`. Its summary
contains task count, eligible candidate count, and ineligible count. Official
SWE resolved verdicts come from the Pro-Lite evaluation commands.

`--no-progress-timeout` selects actual progress supervision. Completed native
model rounds, actual model content or tool argument increments, and completed
tools renew the inactivity timer. HTTP success, response creation, keepalives,
log growth, and snapshot modification times leave the timer unchanged. The
standalone single-agent and workflow generator commands accept these options.

Pro-Lite and the parallel runner select the same policy with
`--workflow-env OPENCOLLAB_EVAL_NO_PROGRESS_TIMEOUT=43200`. With this setting,
the default Solver, task, and controller cumulative time limits give way to the
inactivity timer. An explicit
`--workflow-env OPENCOLLAB_EVAL_GENERATION_WALL_TIMEOUT=SECONDS` retains a
generation wall limit. `OPENCOLLAB_EVAL_CONTROLLER_WALL_TIMEOUT` supplies an
explicit controller wall limit. Official evaluation retains its own test
timeout. Existing processes retain settings loaded at launch.

For gateway streaming observations, set
`--workflow-env OPENCOLLAB_EVAL_MODEL_PROGRESS_PATH=/absolute/path/model-progress.jsonl`.
Each generator subprocess appends its existing `oce-progress/INVOCATION_ID` marker to its
existing User-Agent. The gateway includes that ID as `progress_id` in compact
events. Observers accept shared-file activity only for the exact generator ID.
Each event stores `event`, `epoch`, `call`, and `content_chars`. Actual content
uses `model_content` or `tool_arguments`; a normal completed round uses
`model_completed`. The generic concurrent evaluator uses task-scoped progress
sources or explicitly bound gateway events. A gateway progress binding covers
one active task per invocation.

The progress status records generation and tool activity, candidate capture,
and scoring as separate stages. Generation duration ends at the native run's
return, ahead of candidate capture and report writing. A cooperative inactivity
stop retains native snapshots and journals, then uses the existing failed
capture recovery procedure to select a proven candidate for official evaluation.

### `oc-eval swe-v1-prolite`

This command runs one bounded remote Pro-Lite slice through generation and
official evaluation. The main option groups are shown below.

| Group | Options |
| --- | --- |
| Remote runtime | `--host`, `--ssh-command`, `--remote-python`, `--remote-root`, `--remote-runtime-repo` |
| Task selection | `--start-index`, `--limit`, `--run-id`, `--base-run-dir` |
| Solver | `--workflow`, `--agent-profile`, `--model-name`, `--llm-model`, `--llm-provider`, `--budget`, `--max-steps` |
| Model identity | `--context-window`, `--temperature`, `--top-p`, `--max-output-tokens` |
| Provider transport | `--remote-proxy-base-url`, `--local-proxy-base-url`, `--proxy-env-file`, `--remote-api-env-file` |
| Evidence limits | `--max-task-starts`, `--max-eval-attempts`, `--checkpoint-interval`, `--eval-container-bind-timeout` |
| Output | `--json-output`, `--markdown-output`, `--parent-output-dir` |
| Maintenance | `--dry-run`, `--eval-only`, `--no-sync-runtime`, `--expected-runtime-tree-sha256` |

`--eval-container-bind-timeout` controls how long the runner waits for Docker
to publish the official-evaluation container ID. Its default is 30 seconds and
accepted values range from 1 through 300 seconds. The setting is part of the
official-evaluation run identity, so a report created with another value is not
reused.

Run the installed help before constructing automation.

`--agent-profile single2` selects the Single2 runtime for every agent role of the chosen OpenCollab workflow. Workflow selection remains independent. G21 uses `--workflow validation-council-dual-coder-contract-v1`. The workflow generator and parallel runner accept the same pair of options. Omitting the profile preserves the existing driver, and `single` explicitly selects that default.

```bash
oc-eval swe-v1-prolite --help
```

### `oc-eval final-report`

This command validates two complete fact reports, their clean-run audit
manifests, the canonical dataset, and all referenced evidence before publishing
JSON, Markdown, TeX, PDF, and a final manifest.

```bash
oc-eval final-report \
  --method-a-report METHOD_A.json \
  --method-a-audit-manifest METHOD_A_AUDIT.json \
  --method-b-report METHOD_B.json \
  --method-b-audit-manifest METHOD_B_AUDIT.json \
  --dataset-file DATASET.jsonl \
  --meeting-date YYYY-MM-DD \
  --author AUTHOR \
  --output-dir DIRECTORY
```

See [final-report.md](final-report.md) for the complete evidence contract.

### `oc-eval rejudge-queue`

This command resumes official evaluation for a bounded list of existing
candidates. The queue plan binds every job to a parent run, task index, run ID,
evaluation directory, and patch SHA-256. Model generation is disabled for
every child process.

```bash
oc-eval rejudge-queue \
  --plan /absolute/path/rejudge-plan.json \
  --output-dir /absolute/path/rejudge-state \
  --workers 2
```

The queue skips an existing terminal report only when its task, record ID,
source patch SHA-256, evaluation patch SHA-256, candidate projection, and
direct test-execution proof all match. Conflicting verdicts fail closed.
Remaining jobs run concurrently within the configured limit, retain the parent
attempt budget, and refresh each parent fact report. Its state file is updated
after every transition, so interrupted queues can be started again with the
same plan.

## Solver coordinator

```bash
python -m opencollab_eval.commands.swe_eval_run --help
```

The coordinator selects `g11`, `g1.1`, `baseTeam`, `TeamPro`, `openhands`, or
`claude-code`, applies its fixed defaults, and delegates to the parallel
Pro-Lite runner. Its own options select the dataset, indices, Solver, workers,
run ID, output directory, and detached process mode. Additional recognized
Pro-Lite options are forwarded to the parallel runner.

Detached mode is a macOS operator convenience implemented through `launchd`.
Direct provider transport can run in the foreground across supported
platforms. Other provider transports use the persistent `launchd` relay by
default. CI and Linux automation should pass `--no-persistent-proxy` and
provide an already managed relay and tunnel.

## Advanced module entrypoints

Advanced commands are installed package modules. Their interfaces are intended
for repository operators and tests, and they can evolve faster than the
top-level CLI.

| Module | Use |
| --- | --- |
| `opencollab_eval.generation.gen_prediction` | Generate one single-agent prediction |
| `opencollab_eval.generation.gen_prediction_workflow` | Generate one workflow prediction |
| `opencollab_eval.generation.gen_prediction_openhands` | Generate one OpenHands prediction |
| `opencollab_eval.commands.swe_g11_parallel_runner` | Coordinate a parallel G1.1-compatible batch |
| `opencollab_eval.commands.swe_eval_layer_report` | Merge bounded evaluation rounds into one fact report |
| `opencollab_eval.commands.swe_rejudge_direct_eval` | Re-evaluate an explicitly bound existing candidate |
| `opencollab_eval.commands.swe_rejudge_queue` | Resume official evaluation for a bounded candidate queue |
| `opencollab_eval.commands.swe_token_cost_summary` | Summarize recorded model usage and configured prices |
| `opencollab_eval.commands.swe_frozen_manifest` | Validate a frozen task manifest before Solver launch |

Invoke a module through the installed interpreter.

```bash
python -m opencollab_eval.generation.gen_prediction_workflow --help
```

Candidate projection helpers, process guards, relay helpers, report renderers,
and sidecar builders are implementation interfaces. Production automation
should call the top-level commands or the documented advanced modules instead
of composing private helpers.

## Exit and result semantics

Argument or validation errors use a nonzero exit. A completed command can also
write task-level technical failures. The generated JSON records each task
outcome, while the process exit code describes the command as a whole.

`resolved`, `unresolved`, and `technical_failed` are mutually exclusive
terminal classifications for an officially evaluated task. Candidate
eligibility from `oc-eval run` is a separate generation classification.
