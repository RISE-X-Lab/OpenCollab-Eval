# SWE Pro-Lite operations

**English** | [简体中文](zh-CN/swe-prolite-operations.md)

This guide runs candidate generation and official evaluation on a remote
worker. Example commands use `evaluator@example-worker` and `/srv` or `/results`
paths as placeholders.

## Runtime topology

The operator starts OpenCollab-Eval on a control machine. The runner connects
to a Linux worker through SSH, synchronizes the current OpenCollab public
runtime and OpenCollab-Eval source tree, verifies their manifest, and starts
one or more run-scoped tasks. The worker needs Docker, Python, the selected
Solver runtime, benchmark images, and run-scoped writable storage.

The trusted Pro-Lite dataset must already exist at
`<remote-root>/datasets/swe-batch-pro-lite/instances.jsonl`. Runtime
synchronization does not upload this evaluator-owned input. `--start-index`
and `--limit` select rows in its stable file order.

Every run should have a unique run ID, output directory, remote base directory,
session prefix, and container ownership label. Credentials are mounted or read
from protected files outside the synchronized source tree.

## Worker preparation

Confirm these conditions before the first run.

| Requirement | Verification |
| --- | --- |
| SSH | Batch-mode SSH reaches the worker |
| Python | `--remote-python` imports OpenCollab-Eval runtime dependencies |
| Docker | `docker info` succeeds for the evaluation account |
| Storage | Remote runtime and run directories are writable |
| Dataset | Trusted JSONL exists at `<remote-root>/datasets/swe-batch-pro-lite/instances.jsonl` |
| Images | Dataset image names resolve to immutable local images |
| Credentials | The selected transport can read a protected environment file |

The low-level slice runner and multi-Solver coordinator accept an explicit
`--remote-python` when the worker system interpreter lacks provider
dependencies. The selected interpreter is forwarded through runtime
synchronization, health probes, generation, and official evaluation.

## Run one bounded slice

The direct Kimi coding profile is the smallest complete example supported by
the current release. The remote environment file contains `KIMI_API_KEY` or
`OPENAI_API_KEY`, is mode `0600`, and is never synchronized from the source
repository.

```bash
oc-eval swe-v1-prolite \
  --host evaluator@example-worker \
  --ssh-command ssh \
  --remote-root /srv/opencollab-eval \
  --remote-runtime-repo /srv/opencollab-eval/runtime \
  --base-run-dir /srv/opencollab-eval/runs/example-001 \
  --run-id example-001 \
  --session-prefix example-001 \
  --start-index 1 \
  --limit 1 \
  --workflow validation-council-solve \
  --model-name kimi-for-coding \
  --llm-model kimi-for-coding \
  --llm-provider openai \
  --context-window 262144 \
  --temperature 1 \
  --top-p 0.95 \
  --max-output-tokens 32768 \
  --workflow-env OPENCOLLAB_THINKING=true \
  --workflow-env 'OPENCOLLAB_THINKING_PARAMS={"thinking":{"type":"enabled","keep":"all"}}' \
  --remote-proxy-base-url https://api.kimi.com/coding/v1 \
  --remote-api-env-file /srv/opencollab-eval/secrets/kimi.env \
  --image-repository registry.example/swe \
  --max-task-starts 1 \
  --max-eval-attempts 1 \
  --json-output /results/example-001.json \
  --markdown-output /results/example-001.md
```

The low-level runner receives the Kimi identity values explicitly. The
multi-Solver coordinator applies the same 262144-token context, temperature 1,
top-p 0.95, maximum output 32768, and retained thinking history as one validated
profile and rejects a conflicting value before generation.

Use `--dry-run` with the same arguments to validate configuration and planned
task selection. A dry run is planning evidence and has no semantic task
verdict.

## Run through the Solver coordinator

The coordinator provides one interface for the bundled Solver profiles.

| Solver | Workflow or adapter | External runtime |
| --- | --- | --- |
| `g11` and `g1.1` | `validation-council-solve` | OpenCollab workflow |
| `baseTeam` | `base-team` | OpenCollab workflow |
| `TeamPro` | `team-pro` | OpenCollab workflow |
| `openhands` | `openhands-external` | OpenHands |
| `claude-code` | external print-mode adapter | Claude Code runtime |

```bash
python -m opencollab_eval.commands.swe_eval_run \
  --indices 1-4 \
  --solver g11 \
  --workers 2 \
  --run-id example-g11-001 \
  --output-dir /results/example-g11-001 \
  --host evaluator@example-worker \
  --remote-root /srv/opencollab-eval \
  --remote-eval-work-root /srv/opencollab-eval/runs \
  --session-prefix example-g11-001 \
  --remote-python /srv/opencollab-eval/venv/bin/python \
  --model-name kimi-k3-g11 \
  --llm-model k3 \
  --llm-provider openai \
  --context-window 1048576 \
  --temperature 1 \
  --top-p 0.95 \
  --max-output-tokens 32768 \
  --remote-proxy-base-url https://api.kimi.com/coding/v1 \
  --remote-api-env-file /srv/opencollab-eval/secrets/kimi.env \
  --image-repository registry.example/swe \
  --max-task-starts 1 \
  --max-eval-attempts 1 \
  --runner-attempts 1
```

The coordinator example uses the validated K3 G11 profile. It binds the exact
`k3` response identity, a 1048576-token context, temperature 1, top-p 0.95,
maximum output 32768, retained thinking, and `reasoning_effort=high`. The
coordinator accepts a comma-separated index list and inclusive ranges.
`--start-index` with `--end-index` is an alternative. Solver defaults are
applied before the remaining options reach the parallel runner.

OpenHands needs Python 3.12 and the packaged `run_openhands_cli.sh` resource.
Claude Code needs its external runtime image and the exact model identity
required by the adapter. Run each external runtime's focused smoke test before
starting a batch.

## Provider transport

Direct Kimi mode reads a credential file already present on the worker and
connects to `https://api.kimi.com/coding/v1`. It bypasses the persistent reverse
proxy.

Other provider configurations use an authenticated local relay and an SSH
reverse tunnel. They require `--proxy-env-file`, `--local-proxy-base-url`,
`--remote-proxy-base-url`, and `--proxy-upstream-base-url` at the coordinator
layer. The remote Solver receives the authenticated relay endpoint, never the
upstream credential.

Provider files should be bounded regular files with mode `0600`. Do not place
them below the source checkout, runtime synchronization root, task workspace,
or output directory.

## Attempts and concurrency

Three limits describe different work.

| Option | Meaning |
| --- | --- |
| `--max-task-starts` | Maximum Solver starts for one task |
| `--max-eval-attempts` | Maximum official evaluation attempts for one candidate |
| `--runner-attempts` | Maximum controller attempts after structured runner failure |

Use value 1 for deterministic smoke tests. Increase a limit only when the
experiment protocol allows the corresponding retry. A provider quota failure,
generation failure, and official evaluation technical failure are recorded
separately and are never converted into unresolved.

The parallel runner can reduce concurrency after shared pressure and recover
after clean tasks. Use `--no-adaptive-concurrency` when fixed concurrency is
part of the experiment protocol. A single task or image failure does not pause
other tasks. A batch pause requires a direct failed probe of shared Docker,
storage, queue, or runtime infrastructure.

## Runtime synchronization

The synchronized runtime contains the OpenCollab public package, the
OpenCollab-Eval package, selected shell resources, and a manifest. Local and
remote source-tree SHA-256 values must match before generation.

`--no-sync-runtime` is valid only with
`--expected-runtime-tree-sha256`. This combination pins an already installed
runtime and rejects any mismatch. It should be used only when the operator has
already synchronized and verified that exact tree.

## Output layout

The local output directory contains the parallel summary, health and preflight
records, per-task reports, and logs. Each remote task directory contains
generation metrics, candidate evidence, the official evaluation workspace,
the official report, and cleanup evidence.

The most important records are shown below.

| Record | Purpose |
| --- | --- |
| `parallel_summary.json` | Batch census and terminal counts |
| `task_<index>_report.json` | Generation, candidate, evaluation, and failure details |
| `final_eval_layer_report.json` | Bound fact report for the selected task set |
| Generation metrics | Record ID, run identity, model identity, and source patch SHA |
| Candidate projection | Base tree, candidate tree, paths, modes, and patch SHA |
| Official report | Target plans, commands, structured proof, cleanup, and verdict |

Always pass explicit external paths through `--json-output`,
`--markdown-output`, and coordinator `--output-dir`. Historical defaults can
resolve below the current working tree. Preserve a completed external run
directory as one evidence unit because its records refer to one another by
identity and hash.

## Resume and evaluation-only maintenance

The runner reuses a result only when the current run identity, record ID,
runtime identity, patch SHA, and required evidence agree. A nearby file name or
an older report is insufficient.

`--eval-only` is a low-level single-slice maintenance option for an existing
candidate. The unified Solver coordinator rejects historical evaluation-only
options so a normal experiment cannot silently skip generation. Record every
authorized re-evaluation in the experiment protocol.

Use `oc-eval rejudge-queue` when several verified candidates need the same
maintenance operation. The queue runs only `--eval-only` children, fixes
`--max-task-starts` and empty-patch retries at zero, checks the planned patch
SHA-256 before accepting a terminal report, and refreshes cumulative parent
reports automatically.

## Completion criteria

A successful batch command can still contain unresolved tasks. Treat the JSON
report as authoritative. A trustworthy resolved row has one bound candidate,
one fresh official workspace, a complete target plan, exact execution proof,
zero technical reasons, quiescent cleanup, and a matching official report.

Continue with [Evaluation integrity](evaluation-integrity.md) for the proof
model and [Troubleshooting](troubleshooting.md) for failure diagnosis.
