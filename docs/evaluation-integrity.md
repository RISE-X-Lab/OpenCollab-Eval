# Evaluation Integrity

**English** | [简体中文](zh-CN/evaluation-integrity.md)

OpenCollab-Eval accepts an evaluation result after its executable evidence is
complete. A task becomes resolved when the declared target tests run against
the candidate produced by the Solver and every required target passes.

The central integrity rule is

```text
same task
+ same run
+ same trusted baseline
+ same candidate tree
+ same patch SHA-256
+ same official workspace
+ complete target execution proof
= eligible terminal verdict
```

Missing links produce a technical failure. A completed test run with valid
evidence may produce an unresolved result when a declared target fails.

## Authorities and trust boundaries

The dataset declares the instance identity, base commit, image, target tests,
and test patch.

The evaluation controller owns runtime synchronization, the trusted baseline,
candidate construction, official workspace preparation, test-plan generation,
evidence parsing, and terminal reports.

The Solver contributes the modifications made in its disposable workspace. Its
Git metadata, self-reported diff, prose, and self-test output are advisory
inputs.

The official-evaluation process executes controller-generated plans and writes
bounded evidence artifacts. The host reads only allowlisted regular files from
the owned output directory after container and process cleanup.

## Public baseline preparation

Before the solver starts, the task image is checked against the dataset
`base_commit`. Runtime dependencies needed by the task are separated from the
candidate view. Repository history is replaced by a deterministic anonymous
commit built from the trusted base tree.

The prepared solver repository has one commit, no remotes, no replacement
references, and no future object history. The preparation record binds the
declared commit, source tree, anonymous commit, image identity, and workspace
digest.

A controller-owned bare Git directory is captured outside the solver-visible
workspace. It contains the trusted baseline commit and tree objects used by
candidate construction. Solver changes to `.git`, aliases, hooks, config,
ignore files, refs, reflogs, replacement refs, or the worktree index cannot
change this controller-owned baseline.

## Workspace classification

Workspace findings are classified by phase, origin, solver visibility, model
change, candidate effect, test effect, evidence effect, identity effect,
repairability, and patch representability.

| Outcome | Meaning |
| --- | --- |
| `allow` | The state has trusted provenance or a verified harmless relationship to the result |
| `sanitize_then_continue` | Disposable residue can be removed from the task copy without changing task semantics |
| `task_technical_failure` | Current task or image state cannot be represented, repaired, or proven harmless |
| `pause_batch` | A direct probe proves failure of shared infrastructure |

Baseline state from the declared commit, public task input, or an allowed
runtime dependency can continue. Cache, log, and temporary residue can be
removed from the disposable copy and rechecked. Unknown solver-visible
baseline content fails the affected image.

After the solver exits, representable current-run changes become candidate
input. Unrepresentable result-affecting changes fail the task. Unknown
cross-run state also fails the task.

## Process quiescence

Candidate extraction begins after solver shutdown and container-wide process
cleanup. The supervisor checks the owned process group and container state.
The workspace becomes candidate input only when no owned process can continue
writing.

An incomplete cleanup sets `execution_quiesced` or `cleanup_quiesced` to false.
The candidate then becomes ineligible and official evaluation cannot produce a
resolved verdict.

## Controller-owned candidate construction

Candidate construction uses the external trusted Git directory, the frozen
solver worktree, and a new temporary index.

The Git environment clears inherited `GIT_*` authority, disables system and
global configuration, disables replacement objects, ignores hooks and
filesystem monitors, uses literal pathspecs, and installs a controller-owned
attribute policy. File contents are hashed without clean filters or text
transformations.

The temporary index starts at the trusted base tree. Tracked changes and
deletions are staged from the final filesystem. Untracked paths are enumerated
with NUL delimiters and classified through the baseline ignore view.
Solver-modified `.gitignore` and `.gitattributes` can enter the candidate as
ordinary file changes, while they cannot hide or transform another candidate
path.

Ignored cache, log, build, and generated runtime paths remain outside the
candidate and stay unopened during candidate selection. Harness control paths
under `.git`, `.opencollab`, and retired artifact prefixes are excluded.

Regular files, deletions, binary data, internal symbolic links, and executable
mode changes use Git-native representations. Hard-linked candidate files are
flattened into independent regular files. Nested repositories have their
solver-owned `.git` marker removed before their visible files are projected.
Outward symbolic links, unreadable candidate files, FIFOs, sockets, devices,
and unsupported Gitlink changes produce a task-scoped construction error.

Every baseline Gitlink receives an explicit preserve, delete, or replacement
projection. A replacement carries the evidence needed to reconstruct the
official workspace.

The resulting `CandidatePatch` records

| Field | Purpose |
| --- | --- |
| `anonymous_base` | Deterministic solver-visible baseline commit |
| `base_tree` | Trusted baseline tree |
| `baseline_sha256` | Digest of the trusted baseline representation |
| `candidate_tree` | Tree created from the temporary candidate index |
| `patch_sha256` | SHA-256 of the binary full-index patch |
| `changed_paths` | Canonical candidate path set |
| `path_modes` | Old and new Git modes for each changed path |
| `untracked_paths` | Selected visible additions |
| census fields | Bounded file and byte counts used during extraction |

The serialized proof states that its base and index came from the controller
and that solver Git metadata and forced ignored files were excluded.

## Fresh official workspace

Official evaluation starts a fresh container from the task image. The same
patch crosses three projection checks.

The source projection applies the patch to the declared dataset commit using a
clean temporary index. It verifies the source base commit, source base tree,
anonymous base, source candidate tree, and patch SHA-256 against the generation
expectation.

The evaluation workspace is then reduced to a public single-commit baseline.
Repository setup commands may prepare dependencies, while a subsequent check
requires the prepared Git head to retain the expected baseline identity. The
prepared projection applies the same patch to this baseline and records its
candidate tree.

After the patch reaches the actual official worktree, verification hashes each
changed file, symbolic link, mode, deletion, and Gitlink into another temporary
index. The computed worktree tree must equal the prepared candidate tree.
Target execution begins after `official_worktree_matches` becomes true.

This two-base design handles images whose public preparation produces a
deterministic anonymous commit while preserving the source-tree identity from
generation.

## Test-plan contract

A Pro-Lite test plan contains a schema, adapter, declared targets, ordered
target batches, ordered commands, proof descriptions, runtime dependencies,
and a verified coverage mode.

The flattened target batches must equal the declared target list. Commands and
proof batches must have the same length as target batches. Empty commands,
`true`, `:`, and other no-op forms are rejected. Unsupported target syntax
produces a technical failure.

`FAIL_TO_PASS` is required. `PASS_TO_PASS` can be empty. When present, both
groups use the same parser-backed execution rules.

## Python evidence

Pytest plans execute through a trusted controller program inside the official
container. The controller runs with the authority needed to reserve the proof
file and prepare a disposable worker home. It launches the Pytest worker under
a separate unprivileged user and a new process session.

The command identity is a SHA-256 over the exact argument vector. The
controller accepts only the expected Pytest launcher and one trusted proof
plugin loaded from the evaluation input directory. Candidate source paths are
released into the worker import view after the proof plugin has loaded.

The plugin sends structured JSONL events through an inherited file descriptor.
The controller requires one session start, one collection finish, one session
finish, ordered per-node phase reports, matching process and Pytest exit
statuses, protocol EOF, and no surviving event writer. It records the worker
PID, worker and controller identities, command SHA-256, return code, and event
stream SHA-256.

A successful batch requires at least one collected node and complete passed
`setup`, `call`, and `teardown` phases for every node matched to the declared
target. Parameterized targets may use a verified parent fallback. The fallback
parent list must be derived exactly from the declared parameterized targets,
and every collected node must remain under the allowed exact target or parent.

Import and collection failures can prove a failing `FAIL_TO_PASS` target when
the structured event stream and candidate-source bindings identify the
declared test. They cannot establish a passing result.

## Go evidence

Go plans use `go test -count=1 -json`. A target written as
`path/to/file_test.go::TestName` produces its own package command and anchored
`-run` expression. Multiple packages remain separate commands, preserving each
package-to-test binding.

Datasets that declare test names without paths can use runtime discovery. The
controller scans test files, emits a structured discovery record for each
package, and then executes the exact tests for that package.

The parser consumes Go JSON events plus narrowly defined compiler diagnostics.
A passing proof requires a `pass` event for every declared test in its bound
package. Dynamic discovery also requires complete ownership of every declared
test and rejects ambiguous package matches.

A failing proof accepts an exact target `fail` event. A build failure qualifies
only when its package, test file diagnostic, declared target, observed command,
and planned command agree. Dependency build output and unrelated package
failures cannot substitute for target execution.

## JavaScript evidence

JavaScript and TypeScript plans use parser-backed adapters for Jest, Mocha, and
ospec. Declared targets are mapped to dataset-selected test files and files
introduced by the judge test patch. Ambiguous aliases, traversal paths, and
unverified file mappings are rejected.

Jest executes explicit test files with JSON, serial execution, verbose output,
and `runTestsByPath`. Mocha groups declared titles by file, constructs anchored
selectors, and requires JSON-stream output. ospec uses a structured launcher
for its declared suites.

The parser checks executed suites and target results against the plan.
Successful process exit alone carries no passing authority. Zero tests,
missing suites, unrelated passing tests, malformed structured output, or a
different command fail the evidence check.

A narrowly bound JavaScript suite-load failure can prove `FAIL_TO_PASS` when a
single declared suite fails to load a module explicitly mocked by that suite's
judge patch. The repository namespace, suite path, missing module, runtime
error count, test count, and command identity must all agree.

## Candidate and run identity

Generation and evaluation records bind the following identities.

| Identity | Binding |
| --- | --- |
| `instance_id` | Sealed benchmark instance |
| `record_id` | Exact prediction and metric pair |
| `run_identity_sha256` | Invocation, solver, model, workflow, and runtime identity |
| `source_patch_sha256` | Patch published by generation |
| `eval_patch_sha256` | Patch accepted by official evaluation |
| `source_base_commit` | Dataset commit used during generation |
| `source_anonymous_base` | Deterministic one-commit solver baseline |
| `source_base_tree` | Trusted source tree |
| `source_candidate_tree` | Candidate tree computed during generation |
| `runtime_tree_sha256` | Synchronized OpenCollab-Eval runtime source |
| evaluation attempt fields | Exact official execution attempt |

The generation patch, candidate proof, prediction row, workflow metric, source
projection, prepared projection, official report, and aggregate row must agree
on their shared identities. A latest-file lookup or matching task name cannot
replace `record_id` and full SHA-256 pairing.

## Verdict semantics

| Terminal result | Required facts |
| --- | --- |
| Resolved | Eligible patch, verified projection and cleanup, safe artifacts, and passing target evidence |
| Unresolved | Bound evidence proves a declared target failure, skip, candidate-caused pre-test failure, or source rejection before an expected candidate tree exists |
| Technical failure | Candidate identity or evaluation state is insufficient to decide correctness |

The evaluator derives its verdict from a durable artifact snapshot. Technical
reasons include unsafe or missing identity evidence, an unsupported plan, an
unknown target outcome, Docker execution failure, non-quiescent processes,
baseline mismatch, projection runtime failure, repository preparation failure,
and directly probed infrastructure failure. Log wording alone does not assign
an infrastructure cause.

`resolved` becomes true only when every declared F2P and P2P target has bound
passing evidence. One bound candidate failure is enough for `unresolved`, even
when later batches did not run. Unknown evidence remains technical until a
separate bound failure already determines the candidate outcome. Container
removal failure after the process group stopped and the workspace froze is
recorded as an operational warning. Failure to quiesce remains technical.
A trusted source rejection proves `unresolved` only when generation did not
already record an expected candidate tree. A later source rejection that
contradicts such a tree, or any rejection against the prepared evaluation base,
is a technical projection inconsistency.

## Evaluation state

| State | Meaning |
| --- | --- |
| `needs_generation` | No prediction exists |
| `generation_active` | A generation session is still owned by the current run |
| `empty_patch_invalid` | Generation ended without a candidate |
| `blocked_missing_metric` | A prediction lacks its terminal workflow metric |
| `blocked_metric_pairing` | Prediction and metric identity cannot be paired |
| `workflow_incomplete` | The workflow has no eligible terminal state |
| `workflow_failed` | Generation ended in a terminal failure or marked the submission ineligible |
| `ready_for_eval` | A non-empty eligible candidate can enter official evaluation |
| `eval_active` | A matching official evaluation is running |
| `eval_done` | A matching official report completed and contains a resolved or unresolved verdict |
| `technical_eval_failed` | Official evaluation ended without trustworthy terminal evidence |

Generation statuses such as timeout, context overflow, cancellation, budget
exhaustion, and patch guard failure remain generation failures. Evaluation
statuses such as missing image, missing specification, empty filtered patch,
driver failure, and failed evidence remain technical evaluation failures.

## Failure scope

| Scope | Effect |
| --- | --- |
| `none` | Continue the current task and batch |
| `task` | Fail the current attempt while other tasks continue |
| `image` | Mark the affected task image invalid while unrelated images continue |
| `shared_infrastructure` | Pause new work after a direct shared-service probe fails |

Text matching cannot promote a failure to shared scope. After a task failure,
the parallel runner may issue fresh probes for Docker, shared storage, queue
state, synchronized runtime, and the configured model endpoint. A confirmed
shared probe failure can pause the batch. A repository-specific anomaly,
provider error without a shared probe, patch failure, or target-test failure
remains local.

## Durable evidence

A direct evaluation report includes the base snapshot, source candidate
projection, prepared candidate projection, generation and evaluation patch
digests, record identity, evaluation specification digest, runtime dependency
identities, exact commands, parser evidence, exit statuses, bounded log tails,
process quiescence, and container cleanup.

The runner publishes only allowlisted output names from its owned temporary
directory. Missing, duplicated, oversized, non-regular, linked, or malformed
artifacts add a technical reason.

Aggregate reports preserve three separate counts. `resolved` counts proven
passes. `unresolved` counts technically complete failing candidates.
`technical_failed` counts tasks without a valid semantic verdict.

Final comparison reports validate dataset identity, task coverage, run
identity, candidate SHA-256, projection evidence, direct execution evidence,
and terminal status before rendering JSON, Markdown, TeX, or PDF outputs.

## Review checklist

An evaluation change is ready for review when its solver-visible input remains
public, its judge fields remain sealed, its candidate comes from the shared
controller-owned constructor, and its official workspace verifies the applied
tree.

Every new test adapter needs a structural plan validator and an independent
parser that proves the declared targets. Every new report needs full identity
binding and bounded artifact reads. Every cleanup path needs observable
quiescence before candidate extraction. Every batch-wide stop needs a direct
probe of the shared dependency it claims has failed.
