# Getting started

**English** | [简体中文](zh-CN/getting-started.md)

This guide covers installation and the first run of the generic candidate
engine, including dataset validation. Official resolved or unresolved verdicts
use the [SWE Pro-Lite operations guide](swe-prolite-operations.md).

## Requirements

The core package supports Python 3.10 through 3.12 and requires OpenCollab
0.5.0 or a later 0.5.x release. Docker is required for container-backed tasks and official SWE-bench
evaluation. The OpenHands extra is available only on Python 3.12.

The evaluator and the framework should come from compatible releases or from
source checkouts at revisions tested together. The repository CI builds both
wheels and verifies the installed boundary.

## Install released or local wheels

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install /path/to/opencollab-0.5.0-py3-none-any.whl
python -m pip install /path/to/opencollab_eval-0.5.0-py3-none-any.whl
oc-eval --version
oc-eval --help
```

Install official SWE-bench support with the package extra.

```bash
python -m pip install '/path/to/opencollab_eval-0.5.0-py3-none-any.whl[swebench]'
```

Install OpenHands support in a Python 3.12 environment.

```bash
python -m pip install '/path/to/opencollab_eval-0.5.0-py3-none-any.whl[openhands]'
```

## Install source checkouts

```bash
git clone https://github.com/RISE-X-Lab/OpenCollab.git
git clone https://github.com/RISE-X-Lab/OpenCollab-Eval.git
cd OpenCollab-Eval
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ../OpenCollab
python -m pip install -e '.[dev,swebench]'
ruff check .
pytest -q
```

An editable OpenCollab checkout is suitable for development. Release and CI
verification should use built wheels, which expose missing package files that
an editable repository path could conceal.

## Inspect a SWE-Batch Pro dataset

`oc-eval inspect` accepts bounded JSONL data. It separates public Solver data
from sealed judge data and replaces every instance ID with a keyed anonymous
identifier.

Create one raw 32-byte identity key in protected evaluator state.

```bash
install -d -m 700 /sealed/opencollab-eval
python -c 'import os,secrets,sys; fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,secrets.token_bytes(32)); os.close(fd)' \
  /sealed/opencollab-eval/identity.key
```

Inspect the dataset.

```bash
oc-eval inspect /data/swe-batch-pro.jsonl \
  --identity-key-file /sealed/opencollab-eval/identity.key \
  --image-repository registry.example/swe
```

Inspection requires an instance identity, repository, and problem statement.
It normalizes any supplied base commit, image, target, and test-patch fields
and keeps them sealed. The smaller inspection contract is sufficient here.
The production runner verifies the full task specification, baseline, image,
and test plan. The image repository option is required when a row contains
only `dockerhub_tag`. The command prints a JSON object with the row count and
anonymous task IDs.

Keep the same key for retries of one experiment. A new experiment may use a new
key. The key, original dataset, and sealed judge fields stay outside Solver
workspaces and source control.

## Run the generic candidate engine

`oc-eval run` accepts an evaluator task JSONL file. This format is separate from
the SWE-Batch Pro dataset format.

```json
{"task_id":"calculator-1","description":"Fix calculator.add and run its tests","repo_path":"/work/calculator","timeout":600,"max_tokens":100000}
```

Supported row fields are shown below.

| Field | Required | Meaning |
| --- | --- | --- |
| `task_id` | Yes | Run-scoped safe task identity |
| `description` | Yes | Goal shown to the Solver |
| `repo_path` | No | Local source repository |
| `docker_image` | No | Container environment |
| `timeout` | No | Per-task wall timeout in seconds |
| `max_tokens` | No | Per-task token budget |
| `extras` | No | Evaluator-owned structured extensions |

Use an absolute `repo_path` for every real local task. When the field is
omitted, the evaluator intentionally uses its current working directory,
which can otherwise make the source checkout itself the Solver target.

Configure the OpenCollab model through environment variables and keep the
credential out of shell history.

```bash
export OPENCOLLAB_MODEL=example-model
export OPENCOLLAB_PROVIDER=openai
read -r OPENCOLLAB_API_KEY < /run/secrets/model-api-key
export OPENCOLLAB_API_KEY
oc-eval run /data/eval-tasks.jsonl \
  --output /results/candidate-run \
  --concurrency 1 \
  --timeout 600
```

The command writes `/results/candidate-run/results.jsonl` and prints candidate
eligibility counts. An eligible candidate still requires an official
evaluation before it can be called resolved or unresolved.

## Next steps

Production remote runs continue in [SWE Pro-Lite operations](swe-prolite-operations.md).
[Evaluation integrity](evaluation-integrity.md) explains result states and
required proof. For a technical failure, follow [Troubleshooting](troubleshooting.md).
