# OpenCollab-Eval

OpenCollab-Eval owns benchmark adaptation, solver isolation, trusted patch
extraction, official evaluation, evidence, batch orchestration, and reporting for
OpenCollab-based software-engineering experiments.

This repository requires OpenCollab 0.2.1 or newer in the 0.2 series. Evaluation production code and tests
import OpenCollab only through `opencollab.sdk` and the temporary
`opencollab.sdk.eval_compat` migration surface.

The package contains benchmark contracts, solver workflows, trusted patch and
process isolation, evidence handling, batch coordination, remote execution,
reporting commands, and the tests for those components. OpenCollab retains its
framework, stable SDK, and framework tests.

```bash
oc-eval inspect path/to/tasks.jsonl --identity-key-file path/to/sealed-identity.key
oc-eval run path/to/tasks.jsonl --model MODEL --provider PROVIDER --output results
```

See [MIGRATION.md](MIGRATION.md) for the ownership map and compatibility-removal
sequence.

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
