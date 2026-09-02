# Batches

One file per paid batch. A batch is one out-dir on the host: one arm, one
card, one ordered slice of a frozen task list, one budget, one set of
switches, two commits. The launcher turns the file into the driver's exact
command, refuses to start when the host does not match it, and leaves a
`batch.json` beside the runs that says what ran.

```
V=.venv/bin/python
$V -m opencollab_eval.commands.batch plan      experiment/batches/<name>.yaml   # local, free
$V -m opencollab_eval.commands.batch preflight experiment/batches/<name>.yaml   # reads the host, changes nothing
$V -m opencollab_eval.commands.batch launch    experiment/batches/<name>.yaml --limit 3
$V -m opencollab_eval.commands.batch status    experiment/batches/<name>.yaml
$V -m opencollab_eval.commands.batch wait      experiment/batches/<name>.yaml [--poll 120] [--timeout 21600]
$V -m opencollab_eval.commands.batch launch    experiment/batches/<name>.yaml   # resumes: done tasks are skipped
$V -m opencollab_eval.commands.batch wait      experiment/batches/<name>.yaml
$V -m opencollab_eval.commands.batch pull      experiment/batches/<name>.yaml
$V -m opencollab_eval.commands.batch report    experiment/batches/<name>.yaml --json /root/oc-batches/<name>.report.json
```

`launch --limit 3` is the paid pre-flight: the first three tasks of the slice,
into the same out-dir. Read them (`pull`, `report`) before launching the rest.
The second `launch` skips what is done because the driver reads its own
`preds-<arm>.jsonl`; a run that is *in* that file is done even if it ended at
its budget. `launch` always runs the pre-flight first and copies only data
(the instance file and `batch.json`) to the host, never source.

## Writing a spec

Copy the nearest existing file and change what differs. Every field is
required; there are no defaults for paid settings, because a default that
drifted looks exactly like one that did not.

| Field | Meaning |
|---|---|
| `name` | The out-dir on the host, the instance file `<name>-instances.jsonl`, the log `<name>.log`, and the local directories `<local_batches_dir>/<name>` (data) and `<name>.launch` (inputs). One name, one batch. A replication is a new name (`<cell>-rep2`), never a second launch of the same one. |
| `host` | A file in `experiment/hosts/`: machine facts (paths, proxy, disk, venv). |
| `arm` | One of the driver's arms; see the table below. |
| `rung`, `cell` | Team arm only. `rung` is the paper's name, `cell` the card file `team.handoff.<cell>.yaml`; see the rung table. Name both. They must agree, and a team spec with neither is rejected. Other arms take neither. |
| `suite`, `rows` | A file in `experiment/suite/` and a 1-based inclusive slice of its `order` column. `subset-30` is byte for byte the first 30 rows of `subset-50`, so `subset-30` rows 1–30 and `subset-50` rows 1–30 are the same tasks. Never reorder or hand-edit a suite. |
| `budget_per_seat` | Tokens per seat. The driver multiplies by the number of seats itself: 3 declared roles → `--budget 6000000` in the manifest for `2000000` here; single is one seat; a bundled workflow uses its module's `SEATS`. |
| `max_steps`, `timeout` | The driver's per-run step and wall-clock caps (`timeout` in seconds; the ladder batches use `5400`). |
| `concurrency` | Runs in flight at once. The only field outside the batch identity: a resume may change it. |
| `model_env` | The `.env` naming model, provider and endpoint, relative to the OpenCollab checkout on the host. A second model is a second file (`configs/.env.luna`), never an edit of the first. |
| `env` | Must set `OPENCOLLAB_LLM_STREAM_CHAT`, `OPENCOLLAB_REASONING_EFFORT`, `OPENCOLLAB_WRITE_NUDGE_MODE`, and may set more. **Quote every value**: YAML reads `off`, `on`, `yes`, `no`, `true`, `false` as booleans, and the runtime reads strings. The loader rejects an unquoted boolean. |
| `pins` | Full 40-character shas of the two checkouts the batch runs under. Both must be commits the local checkouts know about (`plan` checks), and the host must be at exactly these with clean trees (`preflight` checks). |
| `note` | Free text, recorded in `batch.json`. |

### Arms

| `arm` | What it is | Card | Predictions file |
|---|---|---|---|
| `single` | One agent, the working tools | none | `preds-single.jsonl` |
| `team` | Three seats, the model decides whether to hand work over | `cell`/`rung` | `preds-team.jsonl` |
| `self-collaboration` | The paper's Dynamic Workflow: roles sequenced by code | none (the driver passes `--workflow`) | `preds-self-collaboration.jsonl` |
| `self-collaboration-reading-analyst` | Its reading variant | none | `preds-self-collaboration-reading-analyst.jsonl` |
| `best-of-n` | Not run as a batch in this study (see the engineering plan) | none | — |

### Rungs

The ladder of §5, on one shared card body. Adjacent rungs differ in one place.

| `rung` | `cell` | What its closing section says |
|---|---|---|
| `primary` | `facts-v2` | Facts only; the choice is left open. The main grid's Team card. |
| `opt-out` | `cmd-optout` | The imperative to delegate, plus a sentence permitting departure from it. |
| `bare` | `cmd-bare` | The imperative alone. |
| `plain` | `cmd-plain` | Bare, plus "how the task is divided is not your decision". No prohibition. |
| `prohibit` | `cmd-prohibit` | Plain, plus "Do not apply the fix yourself". The positive control. |

`default`, `salience`, `weak`, `strong`, `instructed`, `decide-first`,
`opt-out-message`, `role-identity`, `starved`, `pool-disclosed`, `norm` are
cells that exist as team files but are not rungs of the ladder; a spec may
name them as `cell` without a `rung`, and their numbers are not ladder numbers.

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

## When a check fails

Fix the spec or the host so the check passes. Never work around a failed
check with a hand-typed command: every check is a fact about the batch that
the paper will have to state.

Hard rules, from the machine's own rules: never edit a file in the host
checkouts, never `scp` source to the host (code arrives by `git fetch gh
iclr-2027` onto a real commit), never `docker prune`/`rmi` or remove a
container this experiment did not create, never delete an out-dir (it may
hold paid runs), never kill a process you did not start, never print a line
of a `.env` file whose name contains KEY/TOKEN/SECRET/PASSWORD.

| Check | It means | Do this | Do not |
|---|---|---|---|
| `pin opencollab` / `pin opencollab_eval` | The host checkout is at another commit. | If the spec is right: on the host, `git -C ~/oc-team-smoke/<repo> fetch gh iclr-2027 && git checkout --detach <sha>` (the pinned sha must be on GitHub; push from local first if it is not). If the host is right: change the spec's pin, then `plan` again. | Check out a branch name (it moves); edit files on the host. |
| `clean opencollab` / `clean opencollab_eval` | Modified or untracked files under a host checkout. | Look first: `git -C ~/oc-team-smoke/<repo> status --porcelain`. Untracked scratch files: move them out of the checkout. Modified tracked files: someone edited on the host; report which files and what the diff is before doing anything, then restore with `git checkout -- <file>` only once it is understood. | `git clean`/`git checkout -- .` without reading the diff. |
| `import opencollab from pinned checkout` | Python found the package somewhere else, or not at all. | The launcher always sets `PYTHONPATH`; if this fails the checkout is not a package: `ls ~/oc-team-smoke/OpenCollab/opencollab/__init__.py` (and `OpenCollab-Eval/src/opencollab_eval/__init__.py`). A missing file means a broken clone; re-clone from GitHub. | Install anything into the venv (pypi is unreachable and it would shadow the pin). |
| `card bytes …` says `missing` | The file does not exist on the host at that path. | The cell name is wrong, or the pin predates the card. Locally: `git -C /root/git/OpenCollab show <pin>:configs/team.handoff.<cell>.yaml`. If that fails, the pin is too old for this cell. | Copy the card to the host. |
| `card bytes …` differ | The host's copy is not the pinned bytes. Usually the tree is also dirty. | Same as the dirty-tree row. If the tree is clean and bytes still differ, the host is at a different commit with the same short prefix; compare full shas. | — |
| `role prompt digests computed on host` | The pinned OpenCollab on the host cannot load the team file. The detail shows the last line of the traceback. | Run the same import on the host under the launcher's `PYTHONPATH` to read the whole traceback. A team file that references a prompt outside `configs/` is refused by design. | — |
| `model env file present` | `<opencollab_dir>/<model_env>` is not on the host. | For the first model this is `configs/.env`, which exists. For a second model, the file (`configs/.env.luna`) is created on the host by hand by the person holding the key, and only the person: the key never passes through a chat or a repository. | Paste a key anywhere. |
| `model named` | `OPENCOLLAB_MODEL=` is empty in that file. | Whoever owns the file sets it. Record the exact model id in the spec's `note`. | — |
| `docker disk free` | `/mnt` (docker root) is below `min_free_gb`. It has reached 100% before. | Find what this experiment left: `docker ps -a --filter name=oc-gen- --filter name=oc-wf- --filter status=exited` (prefixes: `oc-gen-` single, `oc-wf-` team and workflows, `oc-bon-` best-of-n). Remove only those, by name. Then `docker system df` and `du -sh ~/oc-team-smoke/*` to see what else is large, and report it. | `docker system prune`, `docker rmi`, removing containers with other prefixes, lowering `min_free_gb` to make the check pass. |
| `task images present` | An image the slice needs is not on the host. | Report the image and the instance. The suite's replacement rule lives in `experiment/suite/README.md` and `sampling-manifest.json`; changing the task list is a pre-registration change, not a launch-time fix. | Pull images from the network into a running experiment; drop the row from the CSV. |
| `running-batch check sees a planted process` | `pgrep` on the host cannot see a process whose command line contains the pattern. Every "nothing is running" answer from this host is now untrustworthy. | Check by hand: `ps -eo pid,etime,args \| grep -F '[o]pencollab_eval.generation.gen_prediction_batch'`. If `pgrep` is missing or its output format changed, say so; do not launch until the check is repaired. | Treat the empty list as "nothing running". |
| `no driver already writing this out-dir` | A driver with `--out-dir <name>` is alive. | `wait` for it, then `pull` and `report`. If it is a stale process (check `ps -o pid,etime,args -p <pid>`; a driver whose log has not grown for hours), report it; do not kill a process you did not start. | Launch a second driver into the same out-dir (the manifest and predictions would interleave). |
| `other batches running on the host` (warning) | The machine is shared. | Look at `/mnt` and the load before adding concurrency; the warning is about capacity, not correctness. | — |
| `out-dir` exists without `batch.json` | A hand-launched batch (before 2026-09-02: `tri15`, `think-*`, `cmdplain30`), or a directory made by something else. | Pick another `name`. Those directories stay as they are. | Delete or rename the directory; write a `batch.json` into it by hand. |
| `out-dir` exists with a different spec | Same name, different batch identity (any field but `concurrency`). | If you meant to resume, restore the field you changed. If you meant a new batch, pick a new name. | Force it. |
| `instance file on host` differs | `<name>-instances.jsonl` on the host is not what `plan` builds now. Either the suite or the frame cache changed since the last launch, or a hand-built file sits there. | Compare `batch.json` (`instances.sha256`, `frame_content_sha256`, `suite_sha256`) between the local `.launch` record and the host's copy in the out-dir. A changed suite or cache after a paid run is a real problem: stop and report which changed. | Overwrite the host file to make the check pass. |

## When the launch or the run goes wrong

| Symptom | Where to look | Likely cause |
|---|---|---|
| `driver process not seen 5 s after launch` | `ssh gpu3 tail -20 ~/oc-team-smoke/<name>.log` | The driver exited at once: a wrong argument, a team file it cannot read, or the venv python missing. The log has the traceback. The log is appended across launches, so read its tail, not its head. |
| `ssh … exited 255` | The launcher retries six times with backoff before raising. | The host refuses new connections under load. Wait, then retry the same command; nothing was started if `launch` failed before its last step. |
| Runs `failed` with a provider or infrastructure error | `<out-dir>/logs-<arm>/<instance>/driver.log` on the host, or under the local copy after `pull` | Proxy not reaching the model (the launcher sets it; a change on the network side), a 4xx from the endpoint, a container that would not start. Such runs enter no denominator; `report` lists them as excluded. A batch with many of them in a row is stopped by hand and reported, not resumed blindly. |
| Runs `stopped` | `report`'s `cap` column | A seat hit its budget. Valid: it counts in every denominator. |
| `status` shows the driver alive but counts not moving for hours | Log tail and `docker ps` | A run stuck inside its container past `timeout` is killed by the driver; if nothing moves for longer than `timeout`, report it with the log tail. |
| `MISSING FROM METRICS` in `report` while the driver is alive | — | Not finished yet. A run is not data until the driver has written its row. |
| `MISSING FROM METRICS` after the driver exited | `logs-<arm>/<instance>/driver.log` | The generator exited without a metrics row. Read the log; re-`launch` resumes only what has no prediction. |
| `analyst card digests … != EXPECTED` | `batch.json` (`host.role_prompt_sha256`) vs the runs' `role_prompt_sha256` | Runs from another card are in this out-dir. That is a contaminated cell: report it; do not pool. |
| `report` finds no seat data (all zeros) for a team cell | `logs-team/<instance>/trajectories/*/*/agent_*.json` | The runtime did not write agent files, or `pull` was partial. Run `pull` again (rsync is idempotent), then check the path exists on the host. |
| `pull` stops with an rsync error | — | Run it again; rsync resumes. Do not read numbers off a partial copy. |
| The paper's number needs a denominator the report does not print | `report --json` | Every run's status, reason and seats are in the JSON; the exclusion list is by name. |

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
(sha256 `a45b1fe4…6dcd`; the cache built from it has sha256 `c008a795…24eea`,
500 rows). The host cannot reach Hugging Face; the local machine can. If the
cache is missing, `plan` says so and stops. A rebuilt cache must reproduce
the same sha256, or every instance file built from it is a different input.

## Records

- `<local_batches_dir>/<name>.launch/batch.json`: the spec, its digest, the
  instance file digest, the suite and cache digests, the expected card digests
  at the pin, the host's answers (heads, import paths, model, endpoint digest,
  role prompt digests), and every launch with its argv. The same file is
  copied into the out-dir on the host before the driver starts.
- `<local_batches_dir>/<name>/`: the out-dir after `pull` (driver manifest,
  predictions, metrics, logs).

`report` prints the per-run table the paper's ladder is read from: seat
tokens, whether a coder or tester seat delivered (spent tokens *and* produced
a turn), `message_agent` calls, tree snapshots, cap hits; then delivered
over valid runs with a Clopper-Pearson interval, the excluded runs by name,
and whether every run's analyst card digest equals the one the host computed
at pre-flight. For a non-team arm it prints status, tokens and patch size
only: delivery is undefined there, not zero.

## Not covered here

Scoring (the SWE-bench harness in `~/oc-team-smoke/.venv-swebench`, see the
gpu3 notes) and the six-axis adherence reader are separate steps; see the
engineering plan.
