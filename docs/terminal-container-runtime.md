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

Replace the runner's copied `container_backend.py` with these imports:

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

The existing caller can continue constructing environments and candidate ports:

```python
environment = ContainerEnvironment(container_id, workspace, command_artifacts)

async def create_candidate(label):
    container_id = await create_task_container(label)
    return ContainerEnvironment(container_id, workspace, candidate_artifacts / label)

candidates = ContainerCandidates(source_container_id, create_candidate, candidate_artifacts)
```

Pass `environment` and `candidates` through OpenCollab's public environment and
candidate-workspace parameters. Existing tool wrappers, benchmark source
protection, task configuration, and verifier entry points remain caller-owned.
Use the package implementation for new workers when upgrading an existing runner.

The Docker command wrapper requires Linux, Bash, `setsid`, GNU `env` with
`--default-signal` support, and the Docker CLI. Resetting INT and QUIT preserves
foreground signal behavior despite the wrapper's background process. Termination
continues to address the owned process group.

## Regression coverage

The command lifecycle tests use real local subprocesses with a substituted Docker
transport. They reproduce a cancellation transport failure followed by a delayed
process exit, then adopt the same selected candidate. They also cover successful
and nonzero exits, unresolved output, active-command rejection, candidate identity,
and container state. The tests run without model requests or a Docker daemon.

