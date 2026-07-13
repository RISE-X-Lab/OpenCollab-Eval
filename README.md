# OpenCollab-Eval

OpenCollab-Eval owns benchmark adaptation, solver isolation, trusted patch
extraction, official evaluation, evidence, batch orchestration, and reporting for
OpenCollab-based software-engineering experiments.

This repository requires OpenCollab 0.3.x and SDK API v2.
Evaluation production code and tests import capabilities from the focused,
versioned modules under `opencollab.sdk`. The dependency is limited to public
SDK names; compatibility shims and framework internals are excluded by tests.

The package contains benchmark contracts, solver workflows, trusted patch and
process isolation, evidence handling, batch coordination, remote execution,
reporting commands, and the tests for those components. OpenCollab retains its
framework, stable SDK, and framework tests.

```bash
oc-eval inspect path/to/tasks.jsonl --identity-key-file path/to/sealed-identity.key
oc-eval run path/to/tasks.jsonl --model MODEL --provider PROVIDER --output results
```

After two 100-task runs have terminal fact reports and clean-run audit
manifests, publish their comparison with `oc-eval final-report`. The command
validates the complete census and evidence bindings before atomically writing a
JSON model, Markdown, TeX, PDF, and publication manifest. See
[docs/final-report.md](docs/final-report.md) for the input contract and example.

See [MIGRATION.md](MIGRATION.md) for the repository ownership map and SDK
boundary.

Release compatibility is verified from built artifacts rather than editable
source trees. Build both wheels, then run
`scripts/verify_wheel_contract.sh PATH_TO_OC_WHEEL PATH_TO_EVAL_WHEEL`; the
script installs both distributions in a fresh virtual environment, runs the
full Eval suite against the packaged wheels, checks the SDK contract, and
exercises both packaged CLI entrypoints.

The identity key is an evaluator-owned file containing exactly 32 random bytes.
Keep it in sealed run state and reuse it for retries of the same batch so public
task IDs remain stable without exposing benchmark instance identifiers.

For development, install the repository and its test dependencies before
running the suite:

```bash
python -m pip install -e /path/to/OpenCollab/opencollab
python -m pip install -e '.[dev,swebench]'
pytest -q
```
