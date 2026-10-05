# OpenCollab-Eval tests

Run the installed development environment from the repository root with
`pytest -q`. Choose a directory to run the tests for one area.

```bash
pytest -q tests/generation
pytest -q tests/evaluation
pytest -q tests/orchestration
pytest -q tests/transport
pytest -q tests/packaging
pytest -q tests/e2e
```

`generation/` covers candidate capture, snapshots, solver backends, and
generation runtime behavior. `evaluation/` covers official test execution,
test plans, evidence, result attribution, and reports. `orchestration/` covers
workflow decisions, runner scheduling, budgets, and command entry points.
`transport/` covers SSH, relays, proxy behavior, remote ownership, and runtime
synchronization. `packaging/` covers public imports, repository documentation,
release metadata, and installed wheels.

`e2e/` contains fast tests for the deterministic SWE scenario and its local
scripted service. These tests use local or substituted container transports.
The Docker and SSH scenario runs through the dedicated E2E script.
`support/` contains shared preparation used by multiple test modules.
`fixtures/` holds reusable fixture assets. Place tests beside the behavior they
exercise and import shared preparation through `tests.support`.

The test package also runs after it is copied outside the source checkout.
`tests.support.paths` resolves copied test assets, installed package modules,
and source-only repository files separately. Set `OPENCOLLAB_EVAL_SOURCE_ROOT`
and `OPENCOLLAB_SOURCE_ROOT` when source checks need checkouts outside the
usual sibling locations. The installed-wheel command is
`scripts/verify_wheel_contract.sh`.

The development setup and paired-wheel commands are in
[CONTRIBUTING.md](../CONTRIBUTING.md). The Docker-backed deterministic scenario
is documented in [the SWE E2E guide](../docs/testing/deterministic-swe-e2e.md).
The local Duo integration test uses scripted HTTP responses, real file tools,
isolated Git candidates, and a hidden scoring test.

```bash
pytest -q tests/evaluation/test_duo_evaluator_smoke.py
pytest -q tests/packaging/test_documentation_contract.py
```

Linux signal tests execute the actual Terminal shell wrappers and report an
explicit skip on systems lacking their Linux utilities. Tests that require the
optional SWE-bench package also report their dependency skip. Read the skip
summary when assessing the scope of a local run.

## Source archives and outputs

Source archives include the complete test suite. Initialize local Git metadata
with `git init --quiet` in an unpacked archive before running repository checks.
Install the development dependencies described in CONTRIBUTING and point
`OPENCOLLAB_SOURCE_ROOT` to the compatible OpenCollab source tree. Then run
`pytest -q` from the unpacked evaluator source directory.

Tests use pytest's system temporary directory for generated workspaces. Save
logs, screenshots and reports outside the source tree. For example,
`pytest -q -p no:cacheprovider --junitxml=/tmp/opencollab-eval-tests.xml`
keeps the report outside the checkout. Small reusable inputs belong in
`fixtures/`, while full evaluation runs use the existing external output paths.
