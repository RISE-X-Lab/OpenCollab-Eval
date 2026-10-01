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

`subset-50.csv` is a draw of its own over the suite's 100 rows (namespace
`subset`), not a prefix of `suite-100.csv`; only `subset-30.csv` is a prefix,
of `subset-50.csv`. So the subset carries no reserve of its own, and the
reserve for anything drawn here is `ordered_draw` rows 101--110.

Used once, on 2026-09-05: `pylint-dev__pylint-4661` (row 39 of `subset-50`,
row 94 of `suite-100`) has an evaluation environment that does not run -- the
benchmark's own gold patch scores `resolved 0/1, infra_failure 1` on it -- so
no arm can be scored there. Its replacement is `ordered_draw` row 101,
`scikit-learn__scikit-learn-26323`, the first row of the reserve and the first
row of the draw that no cell has run. The reserve rows are in no `suite-*.csv`
file; a batch addresses this one as row 282 of `frame-ordered.csv`. Nothing in
this directory was rewritten: the replacement lives in the batch specs
(`experiment/batches/README.md`, *Replacements*).

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
