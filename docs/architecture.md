# OpenCollab-Eval Architecture

**English** | [简体中文](zh-CN/architecture.md)

OpenCollab-Eval is the evaluation owner for solvers built with OpenCollab. It
loads benchmark tasks, exposes only public task data to a solver, constructs a
candidate patch from a trusted baseline, runs target tests in a fresh
evaluation workspace, and publishes evidence-bound terminal results.

OpenCollab supplies the agent runtime, environments, tools, and workflow
decorators. OpenCollab-Eval owns benchmark adapters, solver configurations,
candidate construction, test execution proof, evaluation state, and reports.

## Dependency boundary

The production dependency direction is

```text
opencollab_eval
        |
        v
documented OpenCollab public API
```

OpenCollab-Eval currently imports the following public OpenCollab surfaces.

| Public module | Current production imports |
| --- | --- |
| `opencollab` | `OpenCollab` and `RunResult` |
| `opencollab.environments` | `Environment`, `attach_container`, `docker_environment`, and `worktree_environment` |
| `opencollab.tools` | `BuiltinToolName`, `Tool`, and `builtin_tools` |
| `opencollab.workflows` | `workflow` |

The retired `opencollab.sdk` package and OpenCollab implementation layers such
as `adapters`, `application`, `bootstrap`, `domain`, and `harness` stay outside
this dependency boundary. `tests/test_boundaries.py` defines the narrow
compatibility envelope available to production code and tests, scans imports,
and verifies its public names against the installed OpenCollab package.

This boundary gives OpenCollab-Eval a versioned runtime dependency through
`opencollab>=0.4.1,<0.5`. A change to OpenCollab internals remains invisible here
as long as the documented public API remains compatible.

## Package map

| Package | Responsibility |
| --- | --- |
| `contracts` | Values that cross benchmark, solver, and judge trust boundaries |
| `benchmarks` | Dataset loading, validation, task normalization, and public identity derivation |
| `workflows` | Evaluation-owned solver workflows assembled from the OpenCollab public API |
| `engine` | State, execution, checkpoints, test plans, evidence, candidate projection, and remote primitives |
| `generation` | Solver adapters, disposable workspaces, process quiescence, and candidate construction |
| `commands` | Installed commands for local runs, remote Pro-Lite, rejudging, monitoring, and reports |
| `resources` | Packaged shell entrypoints and container-side helper programs |
| `configs` | Packaged workflow configurations |

Small shared modules at the package root implement bounded report I/O, patch
path parsing, Gitlink handling, runtime configuration, and model usage
accounting.

## Data ownership

A normalized benchmark task has two halves.

`PublicTask` contains the anonymous task identifier, repository name, problem
statement, public hints, and explicitly public metadata. The anonymous
identifier is an HMAC-derived value. Public metadata rejects judge fields and
other sealed values.

`JudgeSpec` retains the original instance identifier, base commit, evaluation
image, `FAIL_TO_PASS`, `PASS_TO_PASS`, and test patch. The evaluation
controller keeps this object outside the solver input.

Together these values form `BenchmarkTask`.

```text
dataset row
    |
    +---- public fields ----> PublicTask ----> solver
    |
    +---- sealed fields ----> JudgeSpec  ----> evaluator only
```

The lower-level Pro-Lite adapter also has typed `TaskSpec`, `WorkspaceSpec`,
`PatchCandidate`, `EvalResult`, and `RunRecord` values. These records make task,
candidate, and official result identity explicit when data moves between
generation, execution, and reporting.

## Execution surfaces

The installed `oc-eval` command provides four user-facing surfaces.

| Command | Purpose |
| --- | --- |
| `oc-eval inspect` | Validate and summarize a SWE-Batch Pro JSONL dataset through the sealed task boundary |
| `oc-eval run` | Run the local headless evaluation engine over task JSONL |
| `oc-eval swe-v1-prolite` | Run a bounded remote Pro-Lite slice with synchronized runtime and direct evaluation |
| `oc-eval final-report` | Validate two complete terminal fact reports and render a bound comparison publication |

Specialized command modules support G1.1 parallel scheduling, OpenHands and
Claude Code adapters, direct rejudging, frozen manifests, token summaries, and
operational monitoring. They are installed Python modules and use package
imports. Legacy shell launchers that cannot produce current isolation evidence
terminate with technical status before starting a solver.

## End-to-end execution

The production SWE path follows this sequence.

```text
validated dataset row
        |
        v
sealed task normalization
        |
        v
local evaluation controller
        |
        +---- runtime tree manifest and SHA-256
        |
        v
synchronized remote OpenCollab and OpenCollab-Eval source
        |
        v
fresh task image and disposable solver workspace
        |
        v
verified single-commit public baseline
        |
        v
OpenCollab workflow or external solver adapter
        |
        v
container-wide process quiescence
        |
        v
controller-owned candidate projection
        |
        v
fresh official-evaluation workspace
        |
        v
parser-backed FAIL_TO_PASS and PASS_TO_PASS execution
        |
        v
identity-bound terminal report
```

The local controller synchronizes the complete required source tree. It writes a
runtime manifest containing the member list, aggregate size, and SHA-256. The
remote side recomputes the same identity before generation. A shared preflight
can bind later task runs to the same runtime tree. This prevents a partial or
stale remote installation from silently evaluating a candidate.

Each generation attempt receives its own run identity, artifact directory,
container ownership record, and disposable repository state. The solver sees a
normal one-commit Git repository for familiar development tools. A separate Git
directory owned by the evaluation controller records the trusted baseline and
never enters the solver-visible mount.

After solver shutdown, the controller verifies process quiescence and freezes
the final workspace view. Candidate construction reads the final files through
the trusted baseline and a temporary index. Solver-owned Git configuration,
references, object history, and index state have no authority over the
candidate identity.

Official evaluation starts from a fresh image workspace. The controller first
projects the patch against the declared dataset commit. It then prepares the
public single-commit workspace, projects the same patch against that prepared
base, applies it, and recomputes the resulting tree from the actual worktree.
Target tests start only after these tree identities agree.

## Solver integration

Evaluation-owned workflows live under `opencollab_eval.workflows`. They use
OpenCollab workflow decorators and tool factories while keeping benchmark
secrets outside workflow arguments.

The built-in workflow solver registry currently names G1.1, BaseTeam, TeamPro,
OpenHands, and Claude Code configurations. A workflow solver delegates agent
lifecycle management to OpenCollab. External solver adapters launch their
tools in the disposable container and return sidecar usage and candidate
evidence to the same generation path.

Every adapter converges on shared candidate construction. Adapter-specific
shell code may launch a process and collect its sidecar. Patch
canonicalization, candidate tree calculation, patch SHA-256, and official
projection remain common evaluation services.

## Runtime and workspace boundaries

The local process owns dataset parsing, credentials, run configuration, remote
runtime synchronization, scheduling, and durable reports.

The solver container owns only the current task workspace and temporary solver
artifacts. It receives public task text and the configured model connection.
Judge targets, reference material, future repository history, and artifacts
from other runs stay outside that workspace.

The official-evaluation container receives the bound candidate patch, judge
test specification, parser-backed test programs, and an allowlisted output
directory. It creates its own public baseline and validates the applied
candidate tree before executing tests.

Generated predictions, trajectories, logs, reports, datasets, patches, and
PDFs belong in run directories outside the source checkout. The repository
contains schemas, code, tests, and reusable documentation.

## State and reporting

Generation and official evaluation remain separate states. A non-empty patch
with a terminal generation metric becomes eligible for official evaluation
only when its submission integrity evidence is valid. Empty patches, incomplete
metrics, identity pairing failures, and failed generation remain distinct task
states.

Official evaluation produces `eval_done` after a technically complete run.
Within that state, `resolved` records whether every declared target has passing
execution proof. Infrastructure, artifact, cleanup, projection, or evidence
failures produce `technical_eval_failed`.

Reports join the task identity, generation record, patch SHA-256, runtime tree,
candidate projection, test plan, parser evidence, container cleanup, and final
verdict. Aggregate summaries count resolved, unresolved, and technical-failure
tasks separately.

## Extending the system

A new benchmark adapter should normalize input into a public solver view and a
sealed judge view. It should keep dataset-specific parsing under `benchmarks`
or `engine.eval_adapter`.

A new workflow should use only documented OpenCollab public imports and live
under `workflows`. Its task input should contain public issue information.

A new external solver should reuse disposable snapshot preparation, process
quiescence, and the shared candidate constructor. Its sidecar should report
usage and solver identity without becoming the authority for patch content.

A new language test adapter should create a deterministic test plan whose
declared targets, command batches, and parser proof batches have a
machine-checkable one-to-one relationship. Arbitrary shell success cannot
establish target execution.

A new report field should derive from durable bounded artifacts and retain the
identity fields needed to join it to the same task, run, candidate, and
evaluation attempt.
