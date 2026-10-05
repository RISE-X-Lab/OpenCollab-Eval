# Terminal container command lifecycle

**English** | [简体中文](zh-CN/terminal-container-runtime.md)

Terminal runners can use
`opencollab_eval.generation.terminal_container_backend` for command execution
and complete-container candidate retention. The caller supplies the containers,
workspace, artifact directory, and candidate factory. The benchmark runner
continues to own task images, resource limits, network policy, source protection,
and official verifier execution.

The backend retains each foreground Docker execution together with its output
collection task. A command is retired after its process has exited and its output
has been collected successfully. This also happens when an outer tool call has
already returned because cancellation failed. Candidate cleanup and adoption
reconcile this state before checking for remaining commands.

Commands that are running or still collecting output prevent adoption. Failed or
cancelled output collection retains an unresolved execution. Candidate identity
must still match the captured diff and an existing running container. Detached
services required by the task remain available in that selected container.

## Connecting an existing Terminal runner

Replace the runner's copied `container_backend.py` with these imports.

```python
from opencollab_eval.generation.terminal_container_backend import (
    CommandResult,
    ContainerCandidates,
    ContainerEnvironment,
    docker,
    inspect,
    state_diff,
)
```

The existing caller can continue constructing environments and candidate ports.

```python
environment = ContainerEnvironment(container_id, workspace, command_artifacts)

async def create_candidate(label):
    container_id = await create_task_container(label)
    return ContainerEnvironment(container_id, workspace, candidate_artifacts / label)

candidates = ContainerCandidates(source_container_id, create_candidate, candidate_artifacts)
```

Pass `environment` as the public OpenCollab constructor's environment and
`candidates` as the workflow call's candidate workspace.

```python
from opencollab import OpenCollab
from opencollab.builtin_workflows import duo

client = OpenCollab(workspace=workspace, environment=environment)
result = await client.workflow(duo, task_inputs, candidate_workspace=candidates)
selected = candidates.selected
```

After adoption, `selected.environment.container` identifies the retained
candidate container. The caller records that identity, scores a dedicated copy,
and removes its owned containers after preserving the result.

Existing tool wrappers, benchmark source protection, task configuration, and
verifier entry points remain caller-owned.
Use the package implementation for new workers when upgrading an existing runner.

The host requires the Docker CLI. The task container requires Linux, Bash,
`setsid`, and GNU `env` with `--default-signal` support. Resetting INT and QUIT
preserves foreground signal behavior despite the wrapper's background process. Termination
continues to address the owned process group.

## Regression coverage

The command lifecycle tests use real local subprocesses with a substituted Docker
transport. They reproduce a cancellation transport failure followed by a delayed
process exit, then adopt the same selected candidate. They also cover successful
and nonzero exits, unresolved output, active-command rejection, candidate identity,
and container state.

```bash
pytest -q tests/generation/test_terminal_command_lifecycle.py \
  tests/generation/test_terminal_candidate_delivery.py
```

[Terminal verifier preparation](terminal-verifier-preparation.md) provides the
scoring entry point for task-provided login checks and fresh output isolation.
