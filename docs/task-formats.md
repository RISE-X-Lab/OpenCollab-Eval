# Task and dataset formats

**English** | [简体中文](zh-CN/task-formats.md)

OpenCollab-Eval accepts two JSONL contracts at different trust boundaries.
`oc-eval inspect` reads a benchmark dataset with public and sealed judge fields.
`oc-eval run` reads generic evaluator tasks that are already prepared for
Solver execution. Each command expects its own schema.

## SWE-Batch Pro dataset

Each nonempty line is one JSON object. The adapter accepts the canonical names
below and a small set of legacy aliases defined in
`opencollab_eval.benchmarks.swe_batch_pro`.

| Field | Visibility | Meaning |
| --- | --- | --- |
| `instance_id` | Sealed | Original benchmark identity |
| `repo` | Public | Repository name |
| `problem_statement` | Public | Main issue description |
| `requirements` | Public | Required behavior and acceptance criteria |
| `interface` | Public | Introduced or changed interfaces |
| `base_commit` | Sealed | Trusted source revision |
| `docker_image` | Sealed | Complete evaluation image name |
| `dockerhub_tag` | Sealed | Image tag used with `--image-repository` |
| `FAIL_TO_PASS` | Sealed | Targets that must become passing |
| `PASS_TO_PASS` | Sealed | Regression targets |
| `test_patch` | Sealed | Evaluator-owned test changes |
| `solver_public_hints` | Public | Explicitly approved hints |
| `solver_public_metadata` | Public | Explicitly approved JSON-like metadata |

The normalizer rejects a public hint or metadata value that contains an
instance ID, base commit, image, target, test patch, or another sealed value.
The public task ID is a keyed HMAC-derived identifier such as
`solver-0123456789abcdef0123456789abcdef`.

The adapter always combines `problem_statement`, `requirements`, and
`interface` into the complete Solver task specification. A generation adapter
must not silently drop either of the latter two fields.

`oc-eval inspect` reads at most 64 MiB and requires a raw 32-byte key.

Create the raw key once in evaluator storage. Reuse that key when the same public task identities are required. Replace the dataset path below with your ordered V1 file. The exclusive file creation preserves an existing key on repeated setup.

```bash
export INSPECT_ROOT="$HOME/oc-evaluation/inspect-001"
umask 077
mkdir -p "$INSPECT_ROOT"
python - "$INSPECT_ROOT/identity.key" <<'PY_KEY'
import secrets
import sys
from pathlib import Path
path = Path(sys.argv[1])
with path.open("xb") as stream:
    stream.write(secrets.token_bytes(32))
path.chmod(0o600)
PY_KEY
oc-eval inspect /data/swe-batch-pro.jsonl \
  --identity-key-file "$INSPECT_ROOT/identity.key" \
  --image-repository jefzda/sweap-images
```

The dataset and identity key belong to evaluator storage outside the source repository. The command prints `count` and `public_task_ids` to standard output. Public hints are supplied to Solver execution only after the adapter validates their separation from sealed judge fields.

## Generic evaluator task JSONL

`oc-eval run` accepts one evaluator task object per nonempty line.

```jsonl
{"task_id":"calculator-1","description":"Fix calculator.add and run its tests","repo_path":"/work/calculator","timeout":600,"max_tokens":100000}
```

`task_id` and `description` are required strings. `repo_path` selects a local
repository. `docker_image` selects a container environment. `timeout` and
`max_tokens` override command defaults. `extras` must be a JSON object, and its
`test_patch` value must be a string when present.

Use an absolute `repo_path` for real local tasks. Omitting it deliberately
selects the evaluator process working directory.

The reader accepts at most 64 MiB, 8 MiB per line, and 10000 task rows. The file
must be a regular file. Results are written to `results.jsonl` below the
selected output directory.

This command reports candidate production and submission eligibility. It does
not load the sealed SWE judge contract or create an official resolved verdict.

Replace the repository path with an existing Git repository. Put each object on one physical line when saving JSONL. The following command writes that file outside the checkout and generates a candidate. Set the provider credential and endpoint through `OPENCOLLAB_API_KEY` and `OPENCOLLAB_BASE_URL`, and set `OPENCOLLAB_MODEL` to your model before running it.

```bash
export TASK_REPOSITORY=/absolute/path/to/calculator
export TASK_OUTPUT="$HOME/oc-evaluation/local-task-001"
mkdir -p "$TASK_OUTPUT"
python - "$TASK_OUTPUT/tasks.jsonl" <<'PY_TASK'
import json
import os
import sys
from pathlib import Path
row = {
    "task_id": "calculator-1",
    "description": "Fix calculator.add and run its tests",
    "repo_path": str(Path(os.environ["TASK_REPOSITORY"]).resolve()),
    "timeout": 600,
    "max_tokens": 100000,
}
Path(sys.argv[1]).write_text(json.dumps(row) + "\n", encoding="utf-8")
PY_TASK
oc-eval run "$TASK_OUTPUT/tasks.jsonl" \
  --provider openai --model "$OPENCOLLAB_MODEL" \
  --output "$TASK_OUTPUT/results" --concurrency 1
```

Read the resulting candidate rows in `results/results.jsonl`. Solver tests recorded during generation describe its work. Official benchmark outcomes come from the benchmark runner and its bound target execution.

## Generated records

Generated records describe evaluation output and carry their own identity
requirements.

| Record | Identity requirement |
| --- | --- |
| Generation metrics | Task, run, model, workflow, record ID, runtime tree, source patch SHA |
| Candidate projection | Trusted base tree, candidate tree, changed paths, modes, patch SHA |
| Official report | Instance, record, evaluated patch SHA, image ID, target plans, execution proof |
| Fact report | Ordered task census, generation state, official state, semantic verdict |
| Clean-run manifest | Fact report SHA, runtime identities, evidence-file hashes |
| Final publication manifest | Dataset identity and hashes of every published output |

Repair a failed run by correcting the source environment or repeating an
explicitly authorized stage. This produces new evidence bound to the same
permitted identity while preserving the original records.
