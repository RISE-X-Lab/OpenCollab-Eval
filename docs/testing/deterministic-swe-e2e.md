# Deterministic SWE end-to-end test

**English** | [简体中文](../zh-CN/testing/deterministic-swe-e2e.md)

The deterministic E2E proves the installed evaluation path without contacting
a real model provider. It exercises built OpenCollab and OpenCollab-Eval wheels,
ephemeral SSH, real `rsync`, a local OpenAI-compatible service, a disposable
Git task, Docker, trusted candidate extraction, official target execution,
terminal reporting, and owned-resource cleanup.

## What the test proves

The synthetic repository contains a `calculator.add()` implementation that
subtracts its arguments. Its target test fails at the trusted baseline. The
fake model issues deterministic tool calls that inspect the source, replace
subtraction with addition, run the target, and finish.

The production runner synchronizes both installed source trees through SSH and
records matching local and remote tree identities. The candidate path extracts
one nonempty patch and binds its SHA-256 to the run and record identities. The
official SWE-bench harness applies the same patch in a new workspace, collects
one target, passes it, and reports one resolved task with zero technical
failures.

The image includes baseline file modes, a broken link, ignored caches,
untracked residue, a nested repository, and future Git state used to verify
pre-Solver sanitization. A separate integrity smoke exercises recoverable
residue, task-scoped image rejection, concurrent task isolation, and a
background writer that cannot reach quiescence.

## Fake model contract

The local service implements model listing and Chat Completions. It accepts a
fixed synthetic token and the `kimi-for-coding` identity used by this test
fixture. It validates a 262144-token context, temperature 1, top-p 0.95,
maximum output 32768, and retained thinking history.

This identity belongs to the deterministic fixture. It does not select or
validate every production provider profile.

Requests are written to a redacted run-scoped transcript. Unknown routes,
wrong identity, wrong sampling configuration, malformed requests, and early
service exit fail the run.

## Run locally

The host needs Docker, `sshd`, `ssh`, `ssh-keygen`, and `rsync`. Provide the
OpenCollab source root when it is not the sibling `../OpenCollab` checkout.

```bash
export OPENCOLLAB_SOURCE_ROOT=/path/to/OpenCollab
scripts/run_deterministic_swe_e2e.sh \
  --output /tmp/opencollab-eval-e2e \
  --runs 1
```

The output directory must be empty. Use `--runs 3` for repeated stability
validation. Every repetition receives a new run ID and produces the same
candidate patch from a fresh environment.

## Evidence and cleanup

Each run records runtime synchronization, model transcript, prediction,
generation metrics, candidate identity, production report, independent
official proof, validation summary, and cleanup result. Success requires
matching patch hashes, `resolved=1`, `unresolved=0`,
`technical_failed=0`, one collected target, and complete cleanup.

Cleanup is restricted to processes, containers, images, keys, ports, and
directories carrying the current run ID. The final record proves that the fake
model stopped, owned containers and images were removed, temporary work
disappeared, and real provider variables were absent.

## CI

The `deterministic-e2e` GitHub Actions job builds both wheels, installs SSH and
`rsync`, and runs one repetition under a ten-minute job timeout. Failure
artifacts are retained for fourteen days. Local release validation may run
three repetitions.

Focused unit tests cover runtime digest mismatch before model launch, wrong
model identity, patch digest mismatch, wrong context identity, zero collected
tests, early service exit, and resource residue.
