# SWE-bench generation and batch evaluation

OpenCollab-Eval owns the host-side bridge between OpenCollab solvers and the
official SWE-bench harness. Generation starts an official `sweb.eval` image,
runs the selected solver against the isolated `/testbed` workspace, waits for
container-wide process quiescence, and extracts a bounded patch with trusted
host Git against the anonymous pre-solver snapshot.

Run one single-agent task through the installed module:

```bash
python -m opencollab_eval.generation.gen_prediction \
  --instance-file /path/to/instance.json \
  --output /path/to/predictions.jsonl
```

Run one workflow task through
`python -m opencollab_eval.generation.gen_prediction_workflow`. Remote
Pro-Lite batches use
`python -m opencollab_eval.commands.swe_v1_prolite_runner`; parallel G1.1
coordination uses
`python -m opencollab_eval.commands.swe_g11_parallel_runner`.

Python command modules are launched with `python -m`. They use package imports
from the installed OpenCollab-Eval distribution and do not add repository or
package directories to `sys.path`. The remote v1 runner is the sole bootstrap
exception: after the packaged runner source is embedded on an evaluation host,
it adds that host's synchronized `remote_repo/src` directory before importing
the transferred runtime modules.

The packaged `run_team_batch.sh` and `start_team_run.sh` resources preserve
their explicit legacy gates. They return technical status 125 before starting
a solver because their historical mount design cannot establish the current
isolation and trusted-extraction evidence.
