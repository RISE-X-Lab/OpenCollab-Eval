# Contributing to OpenCollab-Eval

**English** | [简体中文](CONTRIBUTING.zh-CN.md)

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

Build both wheels and verify the packaged contract before release.

```bash
python -m pip install build
wheel_root="$(mktemp -d)"
python -m build --wheel --outdir "$wheel_root/opencollab" ../OpenCollab
python -m build --wheel --outdir "$wheel_root/eval" .
scripts/verify_wheel_contract.sh \
  "$wheel_root"/opencollab/opencollab-0.4*.whl \
  "$wheel_root"/eval/opencollab_eval-0.1.0*.whl
```

The deterministic SWE E2E requires Docker, `sshd`, `ssh`, `ssh-keygen`, and
`rsync`. It uses a local fake model service and no provider credential.

```bash
scripts/run_deterministic_swe_e2e.sh --output /tmp/oce-e2e --runs 1
```

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

Update the operator guide, CLI reference, architecture description, and
integrity documentation whenever a change affects commands, defaults, runtime
topology, evidence, or result semantics. Documentation examples must use
external output paths and placeholders rather than local infrastructure.

Public code, comments, tests, and canonical documentation use English.
Simplified Chinese documentation mirrors live only in root `*.zh-CN.md` files,
under `docs/zh-CN/`, and in `README.zh-CN.md` files beside package READMEs
under `src/opencollab_eval/`. Keep every mirror synchronized with its English
source and preserve the same code blocks and technical identifiers. Commit
summaries, pull request titles, pull request descriptions, and review replies
use Chinese while retaining an English Conventional Commit type.

## Contribution license

By submitting a contribution, you represent that you have the legal right to
provide it. You license the contribution under the
[Mulan Permissive Software License v2](LICENSE), matching the repository
license.

Changes to secret detectors or findings inherited from the trusted base require
a dedicated security review before merge.

Security reports use the private process described in [SECURITY.md](SECURITY.md).
