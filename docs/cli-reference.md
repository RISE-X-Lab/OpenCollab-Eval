# CLI reference

**English** | [简体中文](zh-CN/cli-reference.md)

The installed commands cover common operations. Advanced module entrypoints
are available for repository operators and tests. Run `--help` on the installed
revision for its complete option list.

## Choose a command

| Goal | Entry | Where to read the result |
| --- | --- | --- |
| Run the recommended workflow and official tests | `oc-eval duo` | `parallel_summary.json` and `task_<index>_report.json` |
| Generate patches for prepared local tasks | `oc-eval run` | `results.jsonl` |
| Inspect trusted benchmark rows | `oc-eval inspect` | JSON printed to standard output |
| Score saved candidates again | `oc-eval rejudge-queue` | Queue state and refreshed parent task reports |
| Copy the installed sources for a worker | `oc-eval package-runtime` | `runtime-manifest.json` |
| Publish a completed 100-task comparison | `oc-eval final-report` | Publication directory |

Start a new official evaluation with the [Duo tutorial](swe-prolite-operations.md#run-duo-on-one-linux-worker). The lower-level slice runner and Solver coordinator remain available for existing experiments.

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

### `oc-eval duo`

```text
oc-eval duo --config CONFIG [--indices INDICES] [--workers COUNT]
            [--run-id ID] [--output-dir DIRECTORY] [--dry-run]
```

This command runs the single OpenCollab Duo workflow through the official
evaluator. `workflow` is `duo` and `agent_profile` defaults to `base`, which resolves to `single2`.
The adjudicator reads complete saved evidence through its read-only tool.
`--dry-run` prints the effective configuration. `oc-eval g22` is a command
alias. Model settings, budgets, timeouts and evaluation options use the
existing parallel runner.

The wrapper defaults to local transport, row 1, one worker, and `base` roles resolved to `single2`. It writes reports under `<config-directory>/results/<run-id>`. `--output-dir` overrides that directory. Keep a fixed `--run-id` for continuation. The JSON keys use the parallel parser's names with underscores. `workflow_env` accepts an object or a list of `KEY=VALUE` strings. A CLI override takes precedence over the same JSON setting.

### `oc-eval run`

```text
oc-eval run TASKS_FILE --model MODEL --provider PROVIDER
            [--api-key KEY] [--base-url URL] [--output DIRECTORY]
            [--concurrency COUNT] [--max-tokens COUNT] [--timeout SECONDS]
            [--temperature VALUE] [--top-p VALUE] [--agent-profile PROFILE]
            [--no-progress-timeout SECONDS] [--generation-wall-timeout SECONDS]
```

This command runs the generic evaluator and writes `results.jsonl`. Its summary
contains task count, eligible candidate count, and ineligible count. Official
SWE resolved verdicts come from the Pro-Lite evaluation commands.

The defaults are `--output eval_results`, `--concurrency 4`, `--max-tokens 1000000`, `--timeout 600`, and `--temperature 0.2`. Supply `--model` and `--provider`, or set `OPENCOLLAB_MODEL` and `OPENCOLLAB_PROVIDER`. `OPENCOLLAB_API_KEY` and `OPENCOLLAB_BASE_URL` supply the credential and endpoint. Each task can override its token budget and timeout. Choose an external absolute output directory for real runs.

The summary fields are `tasks`, `eligible_patches`, and `ineligible`. Read each saved row's `patch_produced`, `submission_eligible`, `error`, and `execution_quiesced` before handing its patch to an evaluator. [Task formats](task-formats.md#generic-evaluator-task-jsonl) shows how to create the input file.

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
| Scoring adaptation | `--scoring-adapter-registry` |

`--scoring-adapter-registry` selects an external registry already installed at
an absolute worker path. The host default is
`OPENCOLLAB_EVAL_SCORING_ADAPTER_REGISTRY`. See
[external official-scoring adapters](scoring-adapters.md) for the registry
format, entrypoints, runtime packaging, and receipt fields.

`--eval-container-bind-timeout` controls how long the runner waits for Docker
to publish the official-evaluation container ID. Its default is 30 seconds and
accepted values range from 1 through 300 seconds. The setting is part of the
official-evaluation run identity, so a report created with another value is not
reused.

The low-level defaults select `--start-index 26` and `--limit 10`, with workflow `validation-council-solve`. Pass the intended selection explicitly. Numeric defaults are 16000000 task tokens, 60 steps, 14400 seconds for generation, 15300 seconds per task, 7200 seconds for official evaluation, and 900 seconds per model request. `--max-task-starts` defaults to 3 and `--max-eval-attempts` to 2. Trusted host extraction currently requires `--checkpoint-interval 0`. Choose explicit external report paths.

Run the installed help before constructing automation.

`--agent-profile single2` selects the Single2 runtime for every agent role of the chosen OpenCollab workflow. Workflow selection remains independent. G21 uses `--workflow validation-council-dual-coder-contract-v1`. The workflow generator and parallel runner accept the same pair of options. Omitting the profile preserves workflow role configuration. Explicit `base`, `default`, and `single` select Base, currently `single2`. Standalone single-agent generation defaults to Base and uses `OpenCollab.agent(profile="single2")`. Configuration, metrics, and candidate reuse identity record the resolved name. Historical rows with no profile remain distinct from new Base runs.

```bash
oc-eval swe-v1-prolite --help
```

### `oc-eval package-runtime`

```bash
oc-eval package-runtime --output /results/runtime-001
```

The destination must be fresh. This command copies installed OpenCollab and Eval source modules and packaged resources, records the existing runtime manifest, and prints its output path and file count. Runtime dependencies, Docker images, datasets, and provider files are prepared separately on the worker. See [the evaluation suite](evaluation-suite.md#install-and-package).

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

The default queue concurrency is two workers. [Evaluation-only maintenance](swe-prolite-operations.md#prepare-an-evaluation-only-queue) gives the plan schema and source fields.

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

The parallel runner also reads `OPENCOLLAB_EVAL_CAPACITY_CONTROL_FILE` on the
controller host. The external JSON document supplies `generation_workers` as
the number of active task workers, each of which can issue multiple provider
requests. Effective values range from 1 through `--max-workers`, with larger
positive integers capped at that maximum. A configured control file makes
the initial worker count `--min-workers` and enables one-second updates while
normal or technical recovery tasks are in flight. Invalid reads keep the last
valid value. Active tasks finish after a reduction.

## Advanced Solver names

Duo uses its dedicated top-level configuration command. The coordinator's historical Solver catalog contains the following names. Registered workflows and configured external adapters remain usable. A historical mapping whose target is absent from the installed generator registry needs its original experiment runtime. Choose Duo or a registered workflow for a new run.

| Solver name | Workflow or adapter | Current status |
| --- | --- | --- |
| `g11` | `validation-council-solve` | Registered workflow |
| `g1.1` | `validation-council-solve` | Alias for G11 |
| `g20-exp1` | `evidence-action-council-v1` | Historical mapping, absent from the current generator registry |
| `g20-exp2` | `candidate-tournament-council-v1` | Historical mapping, absent from the current generator registry |
| `g11-wired` | `validation-council-wired-v1` | Historical mapping, absent from the current generator registry |
| `baseTeam` | `base-team` | Registered workflow |
| `TeamPro` | `team-pro` | Registered workflow |
| `openhands` | `openhands-external` | External adapter, requires OpenHands runtime |
| `claude-code` | `openhands-external` | External adapter with Claude Code command template |

The [workflow reference](../src/opencollab_eval/workflows/README.md) lists the current registered workflows. Choose a workflow and agent profile independently. Preserve the original choice when reproducing existing results.

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

Task rows use `task_result.status` with terminal values `resolved`, `unresolved`, and `technical_failure`. Aggregate counts use `technical_failed` for the technical-failure total. Read generation and official evaluation fields beside that status. Candidate eligibility from `oc-eval run` describes generation readiness.
