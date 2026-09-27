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

`e2e/` contains the deterministic SWE scenario and its local fake service.
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
