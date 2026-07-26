# Deterministic SWE End-to-End Test Plan

## Objective

The test proves that one installed OpenCollab-Eval command can synchronize the
installed OpenCollab SDK and the current OpenCollab-Eval sources to an ephemeral
remote host, call a deterministic OpenAI-compatible model, let a real agent edit
a repository, extract and bind the candidate patch, execute the candidate with
the official SWE-bench Docker harness, and publish a terminal resolved report.
The test uses a fixed synthetic credential and a localhost-only model service.

## Topology

The entrypoint is `scripts/run_deterministic_swe_e2e.sh`. It builds the two
wheels, installs them into an isolated environment, creates a run-scoped SSH
key and localhost `sshd`, starts a run-scoped fake OpenAI HTTP service, builds a
small local Docker image, and invokes the installed `oc-eval` command. The
command copies both installed source trees with real `rsync` over SSH. The
remote process uses the installed OpenCollab SDK and OpenCollab-Eval runtime to
run the normal agent workflow against a temporary Git repository.

The synthetic repository contains a `calculator.add()` implementation that
subtracts its arguments. Its target test fails at the trusted baseline. The
fake model issues deterministic tool calls that inspect the source, replace the
subtraction with addition, run the target test, and finish. The resulting patch
is extracted through the production candidate path. The installed
`oc-eval swe-v1-prolite` command performs its normal direct evaluation and
publishes the production JSON and Markdown reports. The test then constructs a
SWE-bench `TestSpec` from the same instance and candidate and calls
`swebench.harness.run_evaluation.run_instance` as an independent official
check. Its evaluation script applies the test patch, runs the exact target test,
rejects zero-test output, and writes a second report bound to the same patch.

The image also contains a trusted broken symlink, an executable file, ignored
cache and build residues, an untracked answer file, a nested repository, and a
future commit reference. Before editing, the fake model runs a real shell probe
that proves the disposable workspace retained the trusted baseline state while
removing every readable residue and reducing Git history to one anonymous
commit. The original image remains unchanged.

## Integrity Docker Smoke

The shell entrypoint first runs `e2e.integrity_docker_smoke` through the
installed wheel environment. Three disposable containers execute concurrently.
Two contain recoverable tracked drift and the full residue set, then complete
trusted patch extraction. The third contains a baseline symlink to a readable
answer and must fail with image scope without affecting the other tasks. A
fourth container uses a PID 1 supervisor that continuously replaces killed
background writers. Stable quiescence must fail before patch publication and
the structured result must retain task scope.

The smoke report records each scenario, its failure scope or patch SHA-256,
the verified base commit, elapsed time, and cleanup outcome. Unit tests cover
the same structured failure as it crosses the container helper, host generator,
generation-failure record, remote task result, and one-task summary. Batch pause
remains available only to a direct failed shared-service probe.

## Model Contract

The fake service implements the model-list and Chat Completions endpoints. It
accepts only a fixed fake bearer token and the `kimi-for-coding` model. Every
generation request must carry a 262144-token context identity, temperature 1,
top-p 0.95, maximum output 32768, and enabled thinking with retained thinking
history. The service returns fixed usage and deterministic tool calls. It stores
a redacted JSONL transcript under the run directory and exits with a failure
record for an unknown route, wrong model, missing thinking configuration, wrong
sampling identity, or malformed request.

## Stage Evidence

Each stage appends one immutable record to a run-scoped evidence ledger. The
runtime-sync record contains the local and remote source-tree SHA-256 values,
package versions, file counts, run identifier, and remote destination. The
remote runner is launched only after a second full-tree verification immediately
before generation. Candidate
evidence binds instance ID, record ID, run ID, model name, workflow, runtime
digest, patch SHA-256, context window, sampling parameters, output limit,
thinking configuration, trusted extraction status, and the exact workspace.

Production and independent official evaluation evidence contain the prediction
patch SHA-256, target test command, collected-test count, target-test outcome,
container identity, report path, and report SHA-256. The production JSON and
Markdown reports and the independent official report must agree on one
processed instance, one non-empty patch, one resolved instance, zero unresolved
instances, and zero technical failures. Every output directory starts empty,
so each run produces its own prediction and reports.

## Failure Classification

Runtime source mismatch, model-contract rejection, candidate identity mismatch,
patch digest mismatch, missing direct-test evidence, zero collected tests,
service exit, Docker failure, and cleanup residue are technical failures. A
valid candidate whose target test executes and fails is unresolved. Only an
official target-test pass for the same bound patch is resolved. Every failure
record identifies the stage, run ID, affected instance, machine-readable reason,
and owned artifact paths.

Focused tests exercise remote digest mismatch before model launch, wrong model identity, patch
digest mismatch, wrong context-window identity, zero-test official output, and
early fake-service termination without starting the complete Docker topology.

## Timeouts and Cleanup

The shell entrypoint has a ten-minute hard deadline. Runtime sync, model calls,
remote agent execution, Docker build, and official evaluation each have shorter
stage deadlines. Generation and evaluation retries are disabled, with one
worker, one task start, and one evaluation attempt. Every run receives unique
ports, directories, container names, image tags, SSH keys, and run IDs.

Cleanup records ownership before stopping the run-scoped model service and SSH
daemon, removing the run-scoped container and image, and deleting temporary
directories. It verifies that no owned process, container, listener, or working
directory remains. It never enumerates or deletes resources outside the current
run ID.

## CI Execution

An independent `deterministic-e2e` job builds the exact checked-out OpenCollab
SDK wheel and OpenCollab-Eval wheel, installs both wheels, starts an ephemeral
localhost SSH daemon, and runs the shell entrypoint with Docker. The job timeout
is ten minutes. The target is three to five minutes on a warm runner and less
than eight minutes after a cold image build. On failure it uploads the redacted
model transcript, stage ledger, runtime proof, prediction, metrics, official
report, and cleanup record.

## Local Validation

Local validation runs Ruff, the complete pytest suite, the two-wheel contract,
and three consecutive deterministic end-to-end executions. Each execution must
use a different run ID and empty output directory, report `resolved=1`,
`unresolved=0`, and `technical_failed=0`, and produce matching candidate and
official patch hashes. The validation record captures stage timings, total
duration, target command, patch digest, official report digest, and cleanup
result for all three runs.

## Expected File Scope

Production changes are limited to compact runtime-sync and structured integrity
proofs plus an installed CLI entrypoint that composes existing generation,
candidate, official-eval, and report APIs. Test support lives under `tests/e2e/`,
the shell driver lives under `scripts/`, and CI changes remain in
`.github/workflows/ci.yml`. Fixtures contain only source and configuration text.
Runtime repositories, Git metadata, logs, Docker exports, predictions, and
generated reports remain untracked.
