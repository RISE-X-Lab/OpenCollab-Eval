# Frozen task suite

The task lists the main grid is run on. These files are pre-registration
artifacts, not caches: a run cites the file it drew its tasks from, so a
different seed or a different frame is a new pre-registration rather than an
update to this one. Nothing here is regenerated after a paid run exists.

| File | What it is |
|---|---|
| `frame-verified-500.csv` | The sampling frame: every SWE-bench Verified instance with its repository and the benchmark's own difficulty annotation. Identifiers only -- the task content is fetched from the benchmark, not stored here. |
| `suite-100.csv` | The suite, in the order it was drawn. |
| `subset-50.csv` | The replication subset, in the order it was drawn. |
| `subset-30.csv` | The first 30 rows of `subset-50.csv`, byte for byte: the ones run first. |
| `frame-ordered.csv` | The whole frame in one seeded order (seed 20260901, namespace `frame`, no cap, no strata), with the image column, for the arms that run the frame whole. A prefix of it is a random subsample of the frame, which is what lets an arm stopped early keep its pairing. Every instance whose image was absent at pre-flight is in `frame-ordered-manifest.json` with its reason (none were, on gpu3, 2026-09-03). |
| `sampling-manifest.json` | Seed, cap, frame digest, per-stratum counts in frame and draw, every instance skipped at pre-flight with its reason, and the digests of the code that produced all of the above. |

The short subset is written as a prefix of the long one rather than as its own
draw so that the subset can be grown later without invalidating a run already
paid for: extending it appends rows and moves none. The same shape is why the
suite is read off an ordered list of 110 -- a container image that will not
start is not a random event, so its replacement has to have been chosen before
anyone knew which image would fail.

Regenerate (only when no run has been paid for yet):

```
python -m opencollab_eval.commands.draw_task_suite \
  --frame experiment/suite/frame-verified-500.csv \
  --out-dir experiment/suite --seed <seed> \
  --images <file of docker image references> --images-host <host>
```

The rule itself lives in `src/opencollab_eval/experiment/task_sampling.py`.

Order the whole frame (only when no frame-wide run has been paid for yet):

```
python -m opencollab_eval.commands.order_frame \
  --frame experiment/suite/frame-verified-500.csv \
  --out-dir experiment/suite --seed <seed> \
  --images <file of docker image references> --images-host <host>
```
