# Task and dataset formats

**English** | [简体中文](https://rise-x-lab.github.io/OpenCollab-Eval/zh-CN/task-formats/)

OpenCollab-Eval accepts two JSONL contracts at different trust boundaries.
`oc-eval inspect` reads a benchmark dataset with public and sealed judge fields.
`oc-eval run` reads generic evaluator tasks that are already prepared for
Solver execution. The formats are not interchangeable.

## SWE-Batch Pro dataset

Each nonempty line is one JSON object. The adapter accepts the canonical names
below and a small set of legacy aliases defined in
`opencollab_eval.benchmarks.swe_batch_pro`.

| Field | Visibility | Meaning |
| --- | --- | --- |
| `instance_id` | Sealed | Original benchmark identity |
| `repo` | Public | Repository name |
| `problem_statement` | Public | Solver goal |
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

`oc-eval inspect` reads at most 64 MiB and requires a raw 32-byte key.

```bash
oc-eval inspect /data/swe-batch-pro.jsonl \
  --identity-key-file /sealed/run/identity.key \
  --image-repository registry.example/swe
```

The dataset, identity key, and resulting sealed task mapping belong to
evaluator state outside the source repository.

## Generic evaluator task JSONL

`oc-eval run` accepts one evaluator task object per nonempty line.

```json
{
  "task_id": "calculator-1",
  "description": "Fix calculator.add and run its tests",
  "repo_path": "/work/calculator",
  "timeout": 600,
  "max_tokens": 100000,
  "extras": {
    "test_patch": ""
  }
}
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

## Generated records

Generated records are output contracts rather than input task formats.

| Record | Identity requirement |
| --- | --- |
| Generation metrics | Task, run, model, workflow, record ID, runtime tree, source patch SHA |
| Candidate projection | Trusted base tree, candidate tree, changed paths, modes, patch SHA |
| Official report | Instance, record, evaluated patch SHA, image ID, target plans, execution proof |
| Fact report | Ordered task census, generation state, official state, semantic verdict |
| Clean-run manifest | Fact report SHA, runtime identities, evidence-file hashes |
| Final publication manifest | Dataset identity and hashes of every published output |

Do not edit generated records to repair a failed run. Correct the source
environment or repeat an explicitly authorized stage so new evidence is
produced and bound to the same permitted identity.
