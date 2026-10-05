# Terminal verifier preparation

**English** | [简体中文](zh-CN/terminal-verifier-preparation.md)

`opencollab_eval.generation.terminal_verifier.run_terminal_verifier` prepares a
retained candidate's scoring copy and then invokes the original verifier once.
Preparation and scoring share the task's existing timeout. CPU, memory, task
instructions, candidate selection, and test contents remain caller-owned.

Use a dedicated scoring container created from the saved candidate. Preserve the
original candidate snapshot before preparation. The scorer should use the same
resource limits as the task and a fresh artifact directory for each attempt.

## Task-specific preparation

For `make-doom-for-mips` and `make-mips-interpreter`, preparation archives an
existing `/tmp/frame.bmp` before isolating that one output. Their original tests
wait for this path to appear after starting the VM. A frame left by an earlier
self-test otherwise ends that wait immediately and can cause premature process
termination or verification of an old image. The archive retains the original
bytes and metadata. Other candidate files and services are preserved.

For `configure-git-webserver`, the caller supplies a trusted `login_probe` shell
command that performs the login promised by the task. An absent probe or failed
login produces a preparation error before official scoring. Task provisioning
supplies the ordinary account and its authentication before generation. This
helper checks that setup. Task provisioning owns package installation, account
groups, and repository permissions. Candidate deployment remains with the
solver and task runner.

Configure probes in the trusted task runner. Use the task's configured SSH
identity file or agent rather than embedding a password or key in command text.
The helper passes the probe through stdin and suppresses its output. It records
the outcome rather than the command or authentication diagnostics. Use a real
login command, including an identity check, instead of a port-only check.

For all other task names, the scoring entry point checks that foreground candidate
commands have settled. It makes no task-specific filesystem changes.

## Calling the scoring entry point

The existing official-verifier adapter remains responsible for running the
original test command, collecting the original reward and reports, and enforcing
the remaining timeout passed to it.

```python
from pathlib import Path

from opencollab_eval.generation.terminal_container_backend import ContainerEnvironment
from opencollab_eval.generation.terminal_verifier import run_terminal_verifier

async def score_candidate(
    task, candidate_copy, task_settings, original_verify, output, preparation_receipt
):
    environment = ContainerEnvironment(
        candidate_copy, task_settings["workspace"], Path(output) / "commands"
    )

    async def verify(remaining_seconds):
        # Run the unchanged official verifier in candidate_copy. Return its raw
        # reward, exit_code, timed_out, and error, together with report paths.
        return await original_verify(
            candidate_copy, timeout_seconds=remaining_seconds
        )

    return await run_terminal_verifier(
        task.name,
        environment,
        Path(output) / "preparation",
        verify,
        timeout_seconds=task_settings["verifier_timeout_seconds"],
        login_probe=task_settings.get("login_probe"),
        preparation_receipt=preparation_receipt,
    )
```

For the login task, also call `prepare_terminal_verifier` on the provisioned
source environment before model generation. Give that check its own artifact
directory. This reveals a missing provided login before spending model tokens.
The scoring entry point checks it again in the scoring copy.

```python
from opencollab_eval.generation.terminal_verifier_preparation import (
    prepare_terminal_verifier,
)

if task.name == "configure-git-webserver":
    await prepare_terminal_verifier(
        task.name,
        source_environment,
        Path(output) / "provided-login",
        login_probe=task_settings["login_probe"],
        timeout=30,
    )
```

## Results and recovery

Supply a fresh `preparation_receipt` dictionary per call to retain backup and
isolation paths as preparation proceeds. External cancellation propagates the
standard `asyncio.CancelledError`. The caller reads and persists this dictionary
in its cancellation handler. Receipt ownership is independent of
cancellation exception identity across Python 3.10, 3.11, and 3.12.

The returned `status` is `normal` when the original verifier supplies a numeric
0 or 1 reward, an integer exit code, a false timeout flag, and no error. Reward
1 also requires exit code 0. Its `reward` preserves the official result, and
`verifier` retains the raw record. A usable reward 1 is P. A usable reward 0 is F,
including after preparation succeeds.

Preparation failures and unusable verifier results return E with
`status="facility_error"`, `reward=None`, and `retry_kind="score_only"`. The
`failure_phase` distinguishes preparation from scoring, and `reason` retains
the specific failure. A preparation failure occurs before the official callback.
Keep the candidate and preparation artifacts, inspect the task environment, and
restore the provided prerequisites before scoring the same candidate in a fresh
copy. Use the original evidence to determine whether a missing service arose
from the image, startup configuration, or candidate.

If preparation consumes two seconds of a 900-second budget, scoring receives at
most the remaining 898 seconds. Timeout or cancellation invokes the environment's
abort path. A failed abort remains visible and disables an automatic score-only
retry until the existing environment has been inspected. Raw verifier errors and
unusable results remain available in the returned record.

A campaign controller retains the original score and links each recovered result
to the same candidate and task attempt. This helper returns one attempt record
to that controller.

## Regression coverage

The Linux signal tests execute the actual `EXEC_WRAPPER` and `CANCEL_EXEC`,
including stdin, process groups, SIGINT delivery, external cancellation, and
internal timeout. They skip explicitly on systems without the required Linux
utilities. They complement the existing delayed-exit tests, which substitute the
Docker transport with a direct Python process.

Preparation tests cover old and absent frames, archive failure, changed files,
unsupported file types, failed login, and unaffected tasks. Scoring tests verify
that a preparation error skips the official callback, a real failing result
remains F, and preparation shares the original verifier budget.

```bash
pytest -q tests/generation/test_terminal_verifier_preparation.py \
  tests/generation/test_terminal_verifier.py \
  tests/generation/test_terminal_command_signals.py
```
