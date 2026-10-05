# Getting started

**English** | [简体中文](zh-CN/getting-started.md)

Start by choosing what you want to run. A task for your own repository uses generic task JSONL and produces a candidate patch. A SWE benchmark run uses an ordered benchmark dataset, task images, and official tests. This guide shows the local candidate path first and links to the complete benchmark setup.

## Install the package

The current evaluator is 0.9.1 and requires OpenCollab 0.9.x (`opencollab>=0.9,<0.10`). Use Python 3.10 through 3.12 on Linux or macOS. The commands below use Python 3.12 and keep the environment beside the two source checkouts.

```bash
mkdir -p "$HOME/oc-evaluation"
cd "$HOME/oc-evaluation"
git clone https://github.com/RISE-X-Lab/OpenCollab.git
git clone https://github.com/RISE-X-Lab/OpenCollab-Eval.git
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e ./OpenCollab
python -m pip install -e ./OpenCollab-Eval
oc-eval --version
oc-eval --help
```

The version command should print 0.9.1 for the evaluator. For installation from built wheels, activate an environment and install both compatible packages.

```bash
python -m pip install /path/to/opencollab-0.9.1-py3-none-any.whl
python -m pip install /path/to/opencollab_eval-0.9.1-py3-none-any.whl
```

Official SWE-bench support requires the corresponding extra and Docker on the Linux worker. OpenHands support uses its own extra in Python 3.12. From the source installation directory, install the extra needed by your run.

```bash
python -m pip install -e './OpenCollab-Eval[swebench]'
# For OpenHands, use Python 3.12.
python -m pip install -e './OpenCollab-Eval[openhands]'
```

## Choose the input

| Intended run | Input | Command |
| --- | --- | --- |
| Work on your own Git repository | Generic task JSONL | `oc-eval run` |
| Inspect a SWE-Batch Pro dataset | Benchmark JSONL and a private identity key | `oc-eval inspect` |
| Generate and officially evaluate Duo patches | Ordered benchmark JSONL, images, and Duo JSON configuration | `oc-eval duo` |
| Run a bounded remote benchmark slice | Prepared worker and benchmark settings | `oc-eval swe-v1-prolite` |

The [task format reference](task-formats.md) describes both JSONL formats. Task inputs, credentials, and results belong outside the source checkout and the repository the Solver will edit.

## Run a local repository task

Prepare an existing Git repository with the source and tests the model needs. Replace `/work/calculator` with its absolute path, and replace the description with your task. Omitting `repo_path` selects the evaluator's current working directory, so set it explicitly.

```bash
export EVAL_ROOT="$HOME/oc-evaluation/eval-data"
umask 077
mkdir -p "$EVAL_ROOT"
cat > "$EVAL_ROOT/tasks.jsonl" <<'TASKS'
{"task_id":"calculator-1","description":"Fix calculator.add and run its tests","repo_path":"/work/calculator","timeout":600,"max_tokens":100000}
TASKS
```

Each nonempty line is one task. `task_id` and `description` are required. The example gives that task a 600-second timeout and a 100000-token budget. Optional `docker_image` and `extras` fields are described in [Task formats](task-formats.md).

Set a model and provider supported by your OpenCollab installation. Enter the API key at the prompt.

```bash
export OPENCOLLAB_MODEL=your-model
export OPENCOLLAB_PROVIDER=openai
export OPENCOLLAB_API_KEY="$(python -c 'import getpass; print(getpass.getpass("API key > "))')"
```

For a custom provider endpoint, set its API base URL before running. Replace the example address with your endpoint.

```bash
export OPENCOLLAB_BASE_URL='https://api.example.com/v1'
```

Run one task at a time for the first execution.

```bash
oc-eval run "$EVAL_ROOT/tasks.jsonl" \
  --output "$EVAL_ROOT/candidate-run" \
  --concurrency 1 --timeout 600
```

## Read the candidate result

The output directory contains `results.jsonl`. Read each task's patch and eligibility fields.

```bash
python - "$EVAL_ROOT/candidate-run/results.jsonl" <<'PY_RESULTS'
import json
import sys
from pathlib import Path
for line in Path(sys.argv[1]).read_text().splitlines():
    if line.strip():
        row = json.loads(line)
        print(row["task_id"], row["patch_produced"], row["submission_eligible"])
PY_RESULTS
```

| Field | Meaning |
| --- | --- |
| `task_id` | Input task identity |
| `patch_produced` | A candidate patch was captured |
| `submission_eligible` | The captured candidate satisfies the requirements for submission |
| `patch` | Captured patch text |
| `error` | Recorded execution or capture error |

The printed command summary contains `tasks`, `eligible_patches`, and `ineligible`. An eligible patch can proceed to evaluation. Official success is established by running the benchmark's actual target tests against the matching candidate. The [evaluation integrity guide](evaluation-integrity.md) explains those requirements, and [Troubleshooting](troubleshooting.md) explains execution and capture failures.

## Inspect a benchmark dataset

Use an existing ordered benchmark JSONL file. Its original IDs and judge fields remain in evaluator storage. Inspection prints anonymous task IDs and a row count.

Create a raw 32-byte identity key once for the experiment.

```bash
mkdir -p "$EVAL_ROOT/secrets"
chmod 700 "$EVAL_ROOT/secrets"
python - "$EVAL_ROOT/secrets/identity.key" <<'PY_KEY'
import os
import secrets
import sys
with open(sys.argv[1], "xb") as stream:
    os.chmod(sys.argv[1], 0o600)
    stream.write(secrets.token_bytes(32))
PY_KEY
```

Reuse that key when inspecting the same experiment again. The exclusive file creation above preserves an existing key. Run inspection with the matching image repository. The example repository is for SWE-bench Pro v1 images.

```bash
oc-eval inspect /path/to/instances.jsonl \
  --identity-key-file "$EVAL_ROOT/secrets/identity.key" \
  --image-repository jefzda/sweap-images
```

Rows must include an instance identity, repository, and problem statement. A row carrying an image tag without a repository prefix needs the image repository option. Inspection reads and normalizes the dataset. The official runner subsequently prepares the task image and executes the full benchmark test plan. Keep the key, original dataset, and sealed judge fields outside the Solver workspace and source control.

## Run official benchmark tests

For Duo, follow [Duo on one Linux worker](swe-prolite-operations.md#run-duo-on-one-linux-worker). It prepares the dataset and images, starts the model relay, and creates the configuration used below. Once that setup is complete, execute one task.

```bash
oc-eval duo --config "$EVAL_ROOT/duo.json" \
  --indices 1 --workers 1 --run-id duo-smoke-001
```

Read the resulting task report and its linked official report before expanding to a batch. [Operations](swe-prolite-operations.md) covers remote slices, other solvers, batch outputs, and saved-candidate recovery. [Evaluation suite](evaluation-suite.md) covers server operation. [CLI reference](cli-reference.md) lists command options.
