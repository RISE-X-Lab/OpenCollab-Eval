# Troubleshooting

**English** | [简体中文](zh-CN/troubleshooting.md)

Start with the generated JSON report. It contains the structured reason,
candidate identity, target proof, and cleanup evidence. Console output supplies
additional diagnostic context.

## Locate the failing stage

For Duo, controller reports default to `<config-directory>/results/<run-id>`. Start with `parallel_summary.json`, then open `task_<index>_report.json` for the affected index. `generation` describes the Solver attempt. `eval` describes official scoring and gives its exact report path. `task_result.status` gives the task classification. The task's stdout and stderr logs sit beside these reports.

The command below reads the tutorial's single-task report. Keep the original run directory intact while investigating.

```bash
python - "$EVAL_ROOT/results/duo-smoke-001/task_1_report.json" <<'PY_DIAGNOSE'
import json
import sys
from pathlib import Path
report = json.loads(Path(sys.argv[1]).read_text())
print("run", report.get("run_id"), "status", report.get("status"))
for row in report.get("rows", []):
    print("index", row.get("index"), "result", row.get("task_result"))
    print("generation", row.get("generation"))
    print("evaluation", row.get("eval"))
PY_DIAGNOSE
```

## Configuration fails before a task starts

For `oc-eval duo` or `oc-eval swe-v1-prolite`, add `--dry-run` to check required configuration values. Generic `oc-eval run` reads its own task schema and arguments.
Pro-Lite needs a worker root, image repository, model name and ID, provider, model endpoint, and complete credential transport. SSH transport also needs a host. Duo derives its session prefix from the run ID. Paths that are required to be absolute are rejected before SSH.

For direct Kimi coding mode, use one validated G11 profile. `kimi-for-coding`
uses a 262144-token context with retained thinking history. `k3` uses a
1048576-token context with `reasoning_effort=high`. Both profiles require the
`openai` provider, the `https://api.kimi.com/coding/v1` endpoint, and a
protected environment file that already exists on the worker.

## Provider probe or generation fails

Inspect the shared model health record and the per-task generation metrics.
Model identity, endpoint identity, thinking configuration, context window,
sampling values, and output limit are checked independently.

Authentication failure and provider quota exhaustion are generation technical
failures. Candidate, unresolved, and official evaluation fields remain unset.
Retry only when the experiment protocol permits a new task start.

For reverse-proxy transport, verify the local authenticated relay, SSH tunnel,
remote relay health endpoint, upstream URL hash, and protected token file.
For direct transport, verify worker DNS, HTTPS connectivity, credential-file
mode, and exact model response identity.

## A local interrupted task fails to restart

Inspect `parallel_summary.json` and the per-task report before continuing a batch. Tasks that have never started remain schedulable. Completed tasks with matching evidence reuse their reports. A task interrupted after its local runner started retains `runner.pid` or `summary.json` in the worker task directory. A repeated launch in that directory raises `RemoteRunnerUnavailable`, including when the saved owner state is `dead`.

Use the saved capture receipt's `recovery_environment` and `recovery_argv` to recover a retained candidate after the original owner has exited. Evaluate an existing verified candidate through the [evaluation-only queue](swe-prolite-operations.md#prepare-an-evaluation-only-queue), setting `source_base_run_dir` to the original task directory and `base_run_dir` to a fresh isolated evaluation directory. The worker error `eval-only source and target base run directories must differ` identifies a plan that uses the same path for both. Preserve the original reports and trajectories while preparing the corrected plan.

When the saved evidence cannot establish a trusted candidate and the Solver-start allowance is exhausted, retain that attempt as a technical failure. Any new generation allowed by the experiment protocol uses a fresh run ID and output directory.

## Runtime synchronization fails

The runner creates a manifest over the synchronized OpenCollab public modules,
OpenCollab-Eval modules, and packaged resources. A mismatch means the worker is
not executing the same source tree as the controller.

Check `--remote-python` first. The interpreter must import the synchronized
runtime and all required provider dependencies. Then compare the local runtime
tree record, remote preflight record, and the immediately pre-generation tree
record.

When using `--no-sync-runtime`, supply the exact previously verified SHA-256
through `--expected-runtime-tree-sha256`. A mismatch still stops the run.

## Docker or image preflight fails

Verify `docker info`, image availability, immutable image ID, configured working
directory, and run-scoped storage. A failure for one image is scoped to that
task or image. A batch pause requires a direct probe showing that shared Docker,
storage, queue, or runtime infrastructure is unavailable.

Cleanup removes only containers and processes with the current run ownership
record. A name collision or an unowned container is reported and preserved.

## Candidate construction fails

Inspect generation metrics, trusted snapshot evidence, candidate projection,
and process-quiescence records.

Common task-scoped causes include an unreadable candidate file, a special file
that Git cannot represent, an outward symbolic link, an untrusted Gitlink
replacement, an oversized patch, a background process that continues to write,
or a candidate tree that cannot be reconstructed from the trusted base.

Ignored caches and logs are classified before opening and remain outside the
candidate. The controller-owned candidate identity is unaffected by Solver
changes to Git configuration, index, references, replacement objects, ignore
files, or attributes.

## Official evaluation is technical failed

Read the `technical_reasons` and `output_artifact_errors` fields in the task
report. Check the source patch SHA, evaluated patch SHA, candidate expectation,
image identity, base commit, target plan, command evidence, proof artifact, and
container exit separately.

The following conditions remain technical failures.

| Condition | Reason |
| --- | --- |
| Empty or unsupported target plan | No executable statement of required work |
| Zero collected tests | No target execution occurred |
| Import or collection failure without bound candidate attribution | Target outcome is unknown |
| Projection or official-worktree application failure | Evaluation state is inconsistent |
| Patch SHA mismatch | Generation and evaluation refer to different candidates |
| Missing or unsafe log | The target proof cannot be verified |
| Non-quiescent cleanup | Repository state can still change |

A container removal failure after proven quiescence is recorded as an operational warning. Source rejection classification depends on the bound candidate and expected tree evidence described below.

A candidate becomes unresolved when structured evidence proves an exact target
failure or skip, a candidate-caused build, setup, import, or dependency
failure, or a bound source rejection before generation records an expected
candidate tree. A contradictory source rejection or any prepared-base
rejection remains technical. A nonzero command without candidate attribution
remains technical.

## A result appears stale

Compare task ID, instance ID, record ID, run ID, full patch SHA-256, runtime
tree SHA-256, official report path, and report hash. Reuse requires all relevant
identities and evidence to agree. File modification time, a shortened hash, or
the newest report in a directory is insufficient.

Use a new output and remote base directory for a new run. Preserve a resumed
run's identity and limits when the protocol authorizes continuation.

## Final report publication fails

`oc-eval final-report` validates the complete task census and every referenced
artifact before replacing a publication. Inspect the failed publication
manifest for the first validation or rendering error.

Missing tasks, duplicate tasks, technical failures, dataset hash mismatch,
method mismatch, report hash mismatch, incomplete direct execution evidence,
unsafe artifact paths, and LaTeX compilation failure all return nonzero.
A previously completed publication remains intact when a later attempt fails.

## Collecting a support bundle

Copy only run-scoped, redacted evidence into a directory outside the source
checkout. Include the controller commit, OpenCollab commit, runtime tree hash,
command with credentials removed, task report, generation metrics, candidate
projection, official report, cleanup evidence, and the smallest relevant log.

Review task text, patches, trajectories, instance IDs, provider metadata, host
names, and filesystem paths before sharing the bundle.
