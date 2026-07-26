# Troubleshooting

**English** | [简体中文](zh-CN/troubleshooting.md)

Start with the generated JSON report. Console output is diagnostic context and
does not replace the structured reason, candidate identity, target proof, or
cleanup evidence.

## Configuration fails before a task starts

Run the same command with `--dry-run` and check every required value. Production
Pro-Lite runs need a host, remote root, image repository, model name, model ID,
provider, remote model endpoint, session prefix, and one complete credential
transport. Paths that are required to be absolute are rejected before SSH.

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
failures. They do not create an empty candidate, unresolved verdict, or
official evaluation result. Retry only when the experiment protocol permits a
new task start.

For reverse-proxy transport, verify the local authenticated relay, SSH tunnel,
remote relay health endpoint, upstream URL hash, and protected token file.
For direct transport, verify worker DNS, HTTPS connectivity, credential-file
mode, and exact model response identity.

## Runtime synchronization fails

The runner creates a manifest over the synchronized OpenCollab public modules,
OpenCollab-Eval modules, and packaged resources. A mismatch means the worker is
not executing the same source tree as the controller.

Check `--remote-python` first. The interpreter must import the synchronized
runtime and all required provider dependencies. Then compare the local runtime
tree record, remote preflight record, and the immediately pre-generation tree
record.

Do not bypass a mismatch with `--no-sync-runtime`. That option requires the
exact previously verified SHA-256 through
`--expected-runtime-tree-sha256`.

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

Ignored caches and logs are classified before opening and do not enter the
candidate. Solver modifications to Git configuration, index, references,
replacement objects, ignore files, and attributes cannot change the
controller-owned candidate identity.

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
| Import or collection failure without bound proof | Target outcome is unknown |
| Patch application failure | Tests did not run against the bound candidate |
| Patch SHA mismatch | Generation and evaluation refer to different candidates |
| Missing or unsafe log | The target proof cannot be verified |
| Non-quiescent cleanup | Repository state can still change |

A nonzero test command becomes unresolved only when structured evidence proves
that the intended declared target executed and failed against the bound
candidate.

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
