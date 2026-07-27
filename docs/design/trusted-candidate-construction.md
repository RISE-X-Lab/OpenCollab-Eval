# Trusted candidate construction

**English** | [简体中文](https://rise-x-lab.github.io/OpenCollab-Eval/zh-CN/design/trusted-candidate-construction/)

Candidate identity is controlled by the evaluator's baseline and the final
Solver-visible filesystem. Solver-owned Git configuration, references, object
database, index, ignore changes, aliases, hooks, and replacement objects have no
authority over the published candidate.

## Baseline authority

Before Solver execution, the evaluator creates one anonymous baseline commit
from the dataset base revision. The Solver receives a convenient one-commit Git
repository. A separate controller-owned Git directory retains the trusted base
tree and is never mounted into the Solver runtime.

Baseline evidence records the dataset commit, anonymous commit, base tree,
workspace digest, task image identity, and synchronized runtime identity.
Remotes, future commits, replacement references, reflogs, untracked host files,
old results, and other task artifacts are absent from the Solver workspace.

## Candidate projection

After the Solver exits, the evaluator reclaims the complete owned process set
and requires stable workspace quiescence. Candidate construction then creates a
new temporary index from the trusted base tree.

Tracked edits and deletions are staged from the final worktree. Untracked paths
are enumerated with NUL-delimited literal names and classified against trusted
baseline ignore rules before any candidate file is opened. Solver changes to
`.gitignore` and `.gitattributes` remain ordinary candidate changes, but they
cannot hide paths or transform bytes during extraction.

Git produces the candidate tree, binary full-index patch, changed path census,
file modes, and patch SHA-256. A shared patch-to-tree projection verifies any
post-extraction filtering and every external Solver sidecar against the same
base tree.

Normal files, binaries, deletions, symbolic links, and executable-bit changes
use Git-native representations. Hard links become independent regular files.
FIFOs, sockets, devices, outward links, unreadable candidate files, and
unreconstructable Gitlink replacements fail the current task.

## Ignored and residual state

Ignored caches, logs, and build outputs never become candidate paths. They are
classified before opening, so a broken link, FIFO, root-owned entry, or mode
`000` below an ignored path does not create a false technical failure.

Readable answer residue, future Git state, host files, other task outputs, or
tracked baseline drift cannot be silently accepted. Recoverable residue is
removed only from the disposable task copy and the sanitized state is checked
again before Solver launch.

## Official evaluation binding

The official evaluator starts from a fresh task image or fresh baseline
workspace. It applies the exact published patch, recomputes the evaluated patch
SHA-256 and candidate tree, and requires them to match generation evidence.

The terminal record binds task identity, record ID, run identity, dataset base,
anonymous base, base tree, candidate tree, source patch SHA, evaluated patch
SHA, runtime tree, image ID, target plan, execution proof, and cleanup result.
Tests can prove only this bound candidate.

## Failure scope

An unreadable or unrepresentable candidate, image anomaly, or task process that
will not quiesce fails that task. A batch pause requires a direct failed probe
of shared Docker, storage, queue, or synchronized runtime infrastructure.
Error-message keywords do not determine failure scope.

## Verification

Table-driven tests cover tracked edits and deletion, untracked and ignored
files, modified ignore rules, unreadable caches and candidate files, binaries,
links, modes, hard links, special files, Gitlinks, nested repositories, Solver
Git mutation, and delayed background writes.

The deterministic Docker smoke confirms that recoverable residue can be
sanitized, readable answer residue is blocked, a continuing writer cannot
publish a candidate, and one task failure does not stop unrelated tasks.

The integrity coverage ledger maps each implemented requirement to its exact
regression test. See [Evaluation integrity](../evaluation-integrity.md) and
[the machine-readable ledger](../integrity-coverage.json).
