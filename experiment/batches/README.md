# Batches

One file per paid batch. A batch is one out-dir on the host: one arm, one
cell, one ordered slice of a frozen task list, one budget, one set of
switches, two commits. The launcher turns the file into the driver's exact
command, refuses to start when the host does not match it, and leaves a
`batch.json` beside the runs that says what ran.

```
V=.venv/bin/python
$V -m opencollab_eval.commands.batch plan      experiment/batches/<name>.yaml   # local, free
$V -m opencollab_eval.commands.batch preflight experiment/batches/<name>.yaml   # reads the host
$V -m opencollab_eval.commands.batch launch    experiment/batches/<name>.yaml --limit 3
$V -m opencollab_eval.commands.batch status    experiment/batches/<name>.yaml
$V -m opencollab_eval.commands.batch wait      experiment/batches/<name>.yaml
$V -m opencollab_eval.commands.batch launch    experiment/batches/<name>.yaml   # resumes: done tasks are skipped
$V -m opencollab_eval.commands.batch wait      experiment/batches/<name>.yaml
$V -m opencollab_eval.commands.batch pull      experiment/batches/<name>.yaml
$V -m opencollab_eval.commands.batch report    experiment/batches/<name>.yaml --json /root/oc-batches/<name>.report.json
```

`launch --limit 3` is the paid pre-flight: three runs into the same out-dir.
Read them (`pull`, `report`) before launching the rest; the second `launch`
skips what is done because the driver reads its own predictions file.

## Writing a spec

Copy the nearest existing file and change what differs. Every field is
required; there are no defaults for paid settings.

| Field | Meaning |
|---|---|
| `name` | The out-dir on the host and the local directory under `local_batches_dir`. One name, one batch; a replication is a new name (`<cell>-rep2`). |
| `host` | A file in `experiment/hosts/`. |
| `arm` | One of the driver's arms (`single`, `team`, ...). |
| `cell` | Team arms only: `team.handoff.<cell>.yaml` in the OpenCollab checkout. |
| `suite`, `rows` | A file in `experiment/suite/` and a 1-based inclusive slice of its `order` column. Rows are the frozen draw; never reorder. |
| `budget_per_seat`, `max_steps`, `timeout`, `concurrency` | The driver's own arguments. Concurrency is not part of the batch identity; everything else is. |
| `model_env` | The `.env` naming model, provider and endpoint, relative to the OpenCollab checkout on the host. A second model is a second file (`configs/.env.luna`), never an edit of the first. |
| `env` | Must set `OPENCOLLAB_LLM_STREAM_CHAT`, `OPENCOLLAB_REASONING_EFFORT`, `OPENCOLLAB_WRITE_NUDGE_MODE`. Quote the values: YAML reads `off` as `False`, and the runtime would read `False` as not-off. The loader rejects an unquoted boolean. |
| `pins` | Full 40-character shas of the two checkouts the batch runs under. The pre-flight refuses a host whose HEAD differs or whose tree is dirty. |

## What the pre-flight checks, and why each one exists

Each check is a failure that happened at least once and was silent.

- **Pins and clean trees.** Batches must name two commits. A dirty tree is a
  third, unnamed version.
- **Import path.** The host venv has an installed copy of OpenCollab from
  another checkout; without `PYTHONPATH` the pinned code never runs and
  nothing reports it. The check asks python where `opencollab` came from.
- **Card bytes.** The team file and every prompt it seats, hashed on the host
  and compared with the same paths at the pinned commit (`git show`).
  The host also computes `declared_role_prompt_digests`, which is what a run
  records; `report` compares runs against it.
- **Model file.** Present, names a model. Only the model, provider and a
  digest of the endpoint are read; never the key.
- **Disk, images.** Docker root free space above `min_free_gb`; every image
  the slice needs is present.
- **Running-batch check with a positive control.** A `pgrep -f` pattern can
  match nothing for two reasons. The pre-flight plants a decoy process and
  requires the pattern to see it before trusting its "nothing running".
- **Out-dir.** Absent, or carrying this spec's `batch.json` (a resume).
  A directory without one is a hand-launched batch and is not mixed into.

## Instance files

`plan` rebuilds `<name>-instances.jsonl` from the suite slice and the frame
content cache every time; its sha256 goes into `batch.json`. The cache is the
benchmark's own records, one JSON object per line, every field as a string,
built once from the SWE-bench Verified parquet:

```
python3 - <<'PY'
import json, pathlib, pandas as pd
df = pd.read_parquet('swebench-verified.parquet')
recs = sorted(({k: (v if isinstance(v, str) else str(v)) for k, v in r.items()} for r in df.to_dict('records')), key=lambda r: r['instance_id'])
out = pathlib.Path.home() / '.cache/opencollab-eval/swebench-verified.jsonl'
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(''.join(json.dumps(r, ensure_ascii=False, sort_keys=True) + '\n' for r in recs), encoding='utf-8')
PY
```

The parquet is `princeton-nlp/SWE-bench_Verified`, `data/test-00000-of-00001.parquet`
(sha256 `a45b1fe4…6dcd`). The host cannot reach Hugging Face; the local machine can.

## Records

- `<local_batches_dir>/<name>.launch/batch.json`: the spec, its digest, the
  instance file digest, the expected card digests, the host's answers
  (heads, import paths, model, endpoint digest), and every launch.
  The same file is copied into the out-dir on the host before the driver starts.
- `<local_batches_dir>/<name>/`: the out-dir after `pull` (driver manifest,
  predictions, metrics, logs).

`report` prints the per-run table the paper's ladder is read from: seat
tokens, whether a coder or tester seat delivered, `message_agent` calls,
tree snapshots, cap hits; then delivered/valid with a Clopper-Pearson
interval, the excluded runs by name, and whether every run's analyst card
digest equals the one the host computed at pre-flight.

## Not covered here

Scoring (the SWE-bench harness in `~/oc-team-smoke/.venv-swebench`) and the
six-axis adherence reader are separate steps; see the engineering plan.
