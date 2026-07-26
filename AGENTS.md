# AGENTS.md

OpenCollab-Eval is the evaluation owner for OpenCollab-based solvers. Public
code, comments, tests, and canonical documentation use English. Simplified
Chinese documentation mirrors live only in root `*.zh-CN.md` files,
`docs/zh-CN/`, and `README.zh-CN.md` files beside package READMEs under
`src/opencollab_eval/`. Every translated page must link to its English source,
preserve code blocks and technical identifiers, and remain structurally
aligned with the canonical page. Commit messages, pull request text, and
review replies use Chinese while retaining an English Conventional Commit type.

The dependency direction is `opencollab_eval -> opencollab public API`.
Production code may use the documented root facade and the public
`opencollab.environments`, `opencollab.tools`, and `opencollab.workflows`
modules. It must never import the retired `opencollab.sdk` package or
`opencollab.adapters`, `opencollab.application`, `opencollab.bootstrap`,
`opencollab.domain`, or `opencollab.harness`.

Evaluation integrity is mandatory. A task is resolved only after the official
target tests executed and passed. Empty test commands, zero collected tests,
missing evidence, patch identity mismatches, and non-quiescent workspaces are
technical failures.

A verifier or gate role must retain an executable probe before it may issue a
passing verdict. Prose-only review and successful no-op commands are never pass
evidence.

Install the repository in the test environment, then run `ruff check .` and
`pytest -q` before committing. New behavior needs tests.
Keep Python modules below 800 lines and new files below 500 KB.

Generated reports, model transcripts, predictions, patches, datasets, runtime
logs, PDFs, and local environment paths stay outside the source repository.
