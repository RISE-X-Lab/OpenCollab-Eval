# Trusted Candidate Construction Plan

## Objective

Candidate construction must depend on a controller-owned baseline and the final
Solver-visible filesystem state. Solver-owned Git configuration, references,
objects, index entries, ignore changes, aliases, hooks, and replacement objects
must have no authority over the candidate identity.

The execution path is a verified dataset commit, a disposable baseline
snapshot, a one-commit Solver repository, a controller-owned candidate
projection, a fresh official-evaluation workspace, and a terminal report bound
to the same candidate.

## Existing Foundation

The current implementation already replaces image history with one anonymous
commit, removes remotes and replacement references, captures an external bare
Git directory before Solver execution, waits for container quiescence, copies a
bounded final workspace, initializes a temporary index with `read-tree`, and
produces a binary full-index patch. Candidate construction already rejects
unrepresentable files and outward links while retaining normal files,
deletions, symlinks, binaries, executable bits, hard-link contents, and explicit
Gitlink projections.

This change preserves that foundation and closes the remaining authority and
evidence gaps. In particular, untracked-file selection must use ignore rules
from the trusted baseline instead of the final Solver worktree. Every adapter
must also use one canonical patch-to-tree projection implementation.

## Trusted Baseline and Ignore View

The controller-owned Git directory contains only the anonymous baseline commit
and its reachable tree objects. It is created before Solver execution and is
never mounted into a Solver runtime. The baseline evidence records the dataset
commit, anonymous commit, base tree, workspace digest, task image identity, and
runtime identity through the surrounding generation record.

Candidate construction creates a temporary index and a temporary trusted Git
control view. Baseline `.gitignore` blobs are overlaid while tracked and
untracked paths are staged. A controller-owned `info/attributes` policy disables
text, filter, ident, and working-tree-encoding transformations. Final Solver
changes to `.gitignore` and `.gitattributes` remain ordinary candidate changes,
but they cannot hide another candidate path or transform its bytes. Git global,
system, repository-private, hook, replacement-reference, and external exclude
settings remain disabled.

The untracked census uses NUL-delimited literal paths. It classifies names with
the trusted ignore view before any candidate file is opened. Ignored cache,
log, and build paths therefore remain unopened even when they contain FIFOs,
broken links, root-owned entries, or mode `000`. A selected candidate file that
cannot be read produces a task-scoped technical failure.

## Candidate Projection

The temporary index starts at the trusted base tree. `git add -u` collects
tracked modifications and deletions. Selected untracked paths are added with a
NUL-delimited literal pathspec. No force-add operation participates in candidate
construction.

Git produces the candidate tree, binary full-index patch, changed-path list,
old and new modes, and patch SHA-256. The extraction proof retains these values
alongside the baseline identity and workspace archive identity. A shared
patch-to-tree helper verifies any post-extraction filtering and external Solver
sidecar against the same Git semantics.

Normal files, binaries, deletions, symlinks, and executable-bit changes use Git
native representations. Hard links become independent regular files. FIFOs,
sockets, and device files fail the current task. Baseline Gitlinks require an
explicit controller projection. A changed Gitlink is accepted only when its
replacement can be represented and reconstructed in the fresh evaluation
workspace.

## Adapter Integration

Single-agent, workflow, OpenHands, Claude Code, and snapshot entrypoints all
call the shared candidate constructor. Shell wrappers retain process launch,
resource cleanup, and sidecar generation only. Patch canonicalization and tree
calculation delegate to the shared Python implementation.

The official evaluator receives the exact published patch and its bound
identity. It starts from a fresh task image or fresh baseline workspace, applies
that patch, and records the evaluated patch SHA-256. The projection separately
binds the dataset base commit, the deterministic anonymous Solver commit, the
shared base tree, and the candidate tree. Existing report validation continues
to require the generation record, candidate identity, evaluation identity, and
direct test evidence to agree.

## Failure Scope

Workspace findings use the shared integrity classifier. Proven baseline state
is allowed. Disposable residue is sanitized in the task copy and rechecked.
An unreadable or unrepresentable candidate fails only the current task. A batch
pause requires a direct failed probe of shared Docker, storage, queue, or
runtime services. Error-message text never determines batch scope.

## Verification

Table-driven tests cover tracked edits and deletions, untracked files, trusted
and Solver-modified ignore rules, unreadable ignored caches, unreadable
candidate files, binaries, symlinks, modes, hard links, special files,
Gitlinks, nested repositories, Solver Git mutation, delayed background writes,
and all four integrity outcomes.

Docker smoke tests prove that a disposable task with legal baseline oddities
and removable residue can finish, while readable answer residue, future Git
state, tracked drift, and continuing background writes are blocked at task
scope. A concurrent smoke confirms that one task failure does not pause its
peers and that a directly observed shared-service failure does pause new work.

Historical shadow validation reuses saved candidates for tasks 7, 11, 21, 32,
33, and 34 without model calls. The old and new projections may differ in patch
ordering, but their applied source trees and candidate identities must match
where the old result was valid. Task 21 must ignore its unreadable Hypothesis
cache while preserving the code fix. Tasks 32, 33, and 34 must proceed through
fresh official evaluation using their existing candidates.

Final validation includes Ruff, the complete pytest suite, the two-wheel
contract, Docker cleanup checks, at least one real SWE-Pro official evaluation,
and a read-only independent review.
