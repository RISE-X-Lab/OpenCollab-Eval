# Contributing to OpenCollab-Eval

Thank you for improving OpenCollab-Eval. The repository owns benchmark
adaptation, solver isolation, candidate construction, official evaluation,
evidence, remote execution, and reporting.

## Development setup

Use Python 3.10 or newer. OpenHands integrations require Python 3.12.

```bash
python -m pip install -e ../OpenCollab
python -m pip install -e '.[dev,swebench]'
ruff check .
pytest -q
```

The checkout of OpenCollab must match the compatible version declared in
`pyproject.toml`. The wheel contract test builds both repositories and verifies
the installed boundary without editable imports.

## Architecture

Production code may use only the documented public OpenCollab API. Imports from
OpenCollab adapters, application services, bootstrap modules, domain modules,
or retired harness code are rejected by boundary tests.

An evaluation may report a resolved task only when the declared target tests
executed and passed in the official harness. Empty test plans, zero collected
tests, missing evidence, patch identity drift, and a non-quiescent workspace
are technical failures.

New behavior requires tests. Keep Python modules at or below 800 lines and new
files at or below 500 KB. Do not commit generated reports, model transcripts,
predictions, patches, datasets, container exports, PDFs, credentials, or local
runtime paths.

Public code, comments, tests, and documentation use English. Commit summaries,
pull request titles, pull request descriptions, and review replies use Chinese
while retaining an English Conventional Commit type.

## Contribution license

By submitting a contribution, you represent that you have the legal right to
provide it. You license the contribution under the
[Mulan Permissive Software License v2](LICENSE), matching the repository
license.

Changes to secret detectors or findings inherited from the trusted base require
a dedicated security review before merge.

Security reports use the private process described in [SECURITY.md](SECURITY.md).
