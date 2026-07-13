# AGENTS.md

OpenCollab-Eval is the evaluation owner for OpenCollab-based solvers. Public code,
comments, documentation, commit messages, and pull request text use English.

The dependency direction is `opencollab_eval -> opencollab.sdk`. Production code
must never import `opencollab.adapters`, `opencollab.application`,
`opencollab.bootstrap`, `opencollab.domain`, or `opencollab.harness`.

Evaluation integrity is mandatory. A task is resolved only after the official
target tests executed and passed. Empty test commands, zero collected tests,
missing evidence, patch identity mismatches, and non-quiescent workspaces are
technical failures.

Install the repository in the test environment, then run `ruff check .` and
`pytest -q` before committing. New behavior needs tests.
Keep Python modules below 800 lines and new files below 500 KB.
