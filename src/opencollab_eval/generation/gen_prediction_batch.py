"""Run every arm over a list of instances, one container at a time.

The single-instance generators each solve one task with one arm. A comparison
needs the same tasks put to every arm, and a pilot of ten or twenty tasks is
long enough that how the batch is ordered and what it does when one task fails
stop being details.

Three decisions are worth stating, because each of them is a way an unattended
batch quietly produces something that cannot be compared:

* **Arms are interleaved inside each instance, not run one arm at a time.**
  The primary estimand is a *paired* difference, so a batch that stops early is
  useful exactly as far as its complete pairs go. Finishing every single-agent
  run first would leave a half-finished batch with no pairs at all.
* **One task's failure does not end the batch.** A container that will not
  start, a provider that refuses, a run that exceeds its wall clock: each ends
  that (instance, arm) and nothing else. The manifest says which, and the exit
  status counts the runs that produced *no prediction* rather than the runs
  that exited non-zero -- a generator exits non-zero whenever a run did not
  finish normally, and a run stopped at its token budget still writes the patch
  it had made.
* **A finished (instance, arm) is never re-run.** Resumption reads the
  predictions file each arm already has and skips what is in it, so an
  interrupted batch is continued by re-issuing the same command.
* **The budget is stated per seat and the pool is computed from it.** A team
  run is given one shared pool and caps each of its ``N`` declared roles at
  ``1/N`` of it, so passing a team the same pool as a solo agent gives every
  seat a third of what that agent gets alone -- and the shortfall is then read
  off the results as something about working in a team. The driver reads ``N``
  out of the team file and multiplies, so the two arms are equal on the axis
  they are meant to be equal on.

Runs are sequential. The task containers are heavy and the machine this runs on
is shared; parallelism here is a decision about somebody else's machine, so it
is not taken by default.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opencollab.teams import declared_role_names

from .gen_prediction_constants import (
    DEFAULT_BUDGET,
    DEFAULT_MAX_STEPS,
    DEFAULT_TIMEOUT,
    MAX_INSTANCE_BYTES,
)

#: Which module runs an arm, and whether it needs a team configuration.
ARM_MODULES: dict[str, str] = {
    "single": "opencollab_eval.generation.gen_prediction",
    "team": "opencollab_eval.generation.gen_prediction_workflow",
}

#: Arms whose generator is the workflow entry point and therefore takes
#: ``--team-config``. Kept separate from ``ARM_MODULES`` so adding a workflow
#: arm that is not the team does not silently inherit the team's configuration.
TEAM_ARMS: frozenset[str] = frozenset({"team"})

MANIFEST_NAME = "manifest.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_instances(source: Path) -> list[dict[str, Any]]:
    """Read instances from a directory of JSON files or from one JSONL file.

    Order is the caller's: a directory is read in sorted filename order and a
    JSONL file in file order, so the same argument always produces the same
    batch order and a resumed batch continues where it stopped.
    """
    if source.is_dir():
        paths = sorted(source.glob("*.json"))
        if not paths:
            raise ValueError(f"no instance JSON files in {source}")
        return [_read_instance(path) for path in paths]
    if source.suffix == ".jsonl":
        instances: list[dict[str, Any]] = []
        with source.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    instances.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{source}:{number} is not JSON") from exc
        if not instances:
            raise ValueError(f"{source} holds no instances")
        return instances
    return [_read_instance(source)]


def _read_instance(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_INSTANCE_BYTES:
        raise ValueError(f"{path} exceeds {MAX_INSTANCE_BYTES} bytes")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "instance_id" not in payload:
        raise ValueError(f"{path} is not an instance record")
    return payload


def completed_instance_ids(predictions: Path) -> set[str]:
    """The instance ids a predictions file already holds.

    A malformed line is not a reason to refuse to continue -- it is a reason
    not to claim its instance was done -- so unreadable lines are skipped.
    """
    if not predictions.is_file():
        return set()
    done: set[str] = set()
    with predictions.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            iid = record.get("instance_id")
            if isinstance(iid, str):
                done.add(iid)
    return done


def pool_for(arm: str, budget_per_seat: int, team_config: Path | None) -> int:
    """The token pool one run of ``arm`` is started with.

    One seat, one solo agent's budget. A single-agent run has one seat and is
    given the figure itself; a team is given it once per role its file
    declares, because the scheduler divides the pool by that same count.
    """
    if arm not in TEAM_ARMS:
        return budget_per_seat
    if team_config is None:
        raise ValueError(f"arm {arm!r} needs --team-config")
    return budget_per_seat * len(declared_role_names(str(team_config)))


def build_command(
    *,
    arm: str,
    instance_path: Path,
    predictions: Path,
    team_config: Path | None,
    budget_per_seat: int,
    max_steps: int,
    timeout: float,
    image: str | None,
    extra: Sequence[str] = (),
) -> list[str]:
    """The exact argv one (instance, arm) is run with.

    ``--image`` is passed explicitly whenever the instance names one. The
    generators derive ``sweb.eval.<arch>.<id>:latest`` from the instance id,
    which is the name the official harness uses but not the name every machine
    has: a host that pulled the images under their published ``swebench/``
    namespace has no image under the derived name at all.
    """
    command = [
        sys.executable,
        "-m",
        ARM_MODULES[arm],
        "--instance-file",
        str(instance_path),
        "--output",
        str(predictions),
        "--budget",
        str(pool_for(arm, budget_per_seat, team_config)),
        "--max-steps",
        str(max_steps),
        "--timeout",
        str(timeout),
    ]
    if image:
        command += ["--image", image]
    if arm in TEAM_ARMS:
        if team_config is None:
            raise ValueError(f"arm {arm!r} needs --team-config")
        command += ["--team-config", str(team_config)]
    command += list(extra)
    return command


def _instance_path(instance: dict[str, Any], staging: Path) -> Path:
    """Write the instance out so the generator reads exactly what we selected."""
    staging.mkdir(parents=True, exist_ok=True)
    path = staging / f"{instance['instance_id']}.json"
    path.write_text(json.dumps(instance), encoding="utf-8")
    return path


def plan_batch(
    instances: Iterable[dict[str, Any]],
    arms: Sequence[str],
    done: dict[str, set[str]],
) -> list[tuple[dict[str, Any], str]]:
    """Instance-major order: every arm of one task before the next task.

    This is what makes a stopped batch usable. The estimand is a difference
    between arms on the same task, so the unit that has to survive an
    interruption is the task, not the arm.
    """
    work: list[tuple[dict[str, Any], str]] = []
    for instance in instances:
        for arm in arms:
            if instance["instance_id"] in done.get(arm, set()):
                continue
            work.append((instance, arm))
    return work


def run_batch(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    instances = load_instances(Path(args.instances))
    if args.limit is not None:
        instances = instances[: args.limit]

    predictions = {arm: out_dir / f"preds-{arm}.jsonl" for arm in args.arm}
    done = {arm: completed_instance_ids(path) for arm, path in predictions.items()}
    work = plan_batch(instances, args.arm, done)

    already = sum(len(ids) for ids in done.values())
    print(
        f"batch: {len(instances)} instances x {len(args.arm)} arms "
        f"= {len(instances) * len(args.arm)} runs; "
        f"{already} already in the predictions files; {len(work)} to run"
    )

    manifest = out_dir / MANIFEST_NAME
    failures = 0
    for index, (instance, arm) in enumerate(work, start=1):
        iid = instance["instance_id"]
        log_dir = out_dir / f"logs-{arm}" / iid
        log_dir.mkdir(parents=True, exist_ok=True)
        instance_path = _instance_path(instance, out_dir / "instances")
        command = build_command(
            arm=arm,
            instance_path=instance_path,
            predictions=predictions[arm],
            team_config=Path(args.team_config) if args.team_config else None,
            budget_per_seat=args.budget_per_seat,
            max_steps=args.max_steps,
            timeout=args.timeout,
            image=instance.get("image") or args.image,
            extra=args.pass_through,
        )
        print(f"[{index}/{len(work)}] {arm} {iid}", flush=True)
        if args.dry_run:
            print("  " + " ".join(command))
            continue

        # The workflow log directory is how a run's trajectory is found again,
        # and it is set per run so two arms of one task cannot overwrite each
        # other's evidence.
        env_note = {"OPENCOLLAB_EVAL_WORKFLOW_LOG_DIR": str(log_dir)}
        started = time.monotonic()
        with (log_dir / "driver.log").open("wb") as sink:
            completed = subprocess.run(
                command,
                stdout=sink,
                stderr=subprocess.STDOUT,
                env={**os.environ, **env_note},
                check=False,
            )
        elapsed = round(time.monotonic() - started, 1)
        ok = completed.returncode == 0
        failures += 0 if ok else 1
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "instance_id": iid,
                        "arm": arm,
                        "returncode": completed.returncode,
                        "seconds": elapsed,
                        "finished_at": _now(),
                        "log_dir": str(log_dir),
                        "command": command,
                    }
                )
                + "\n"
            )
        print(f"  rc={completed.returncode} in {elapsed}s", flush=True)

    missing = [
        (instance["instance_id"], arm)
        for instance, arm in work
        if instance["instance_id"] not in completed_instance_ids(predictions[arm])
    ]
    # Two different questions, and the second is the one that decides whether
    # the batch has to be re-run. A generator exits non-zero whenever the run
    # did not finish normally -- a token budget spent, a step ceiling reached --
    # and it writes its prediction first, so a "failed" run of that kind has
    # produced exactly what the comparison needs. What actually costs a row is
    # a run that wrote no prediction at all.
    if failures:
        print(f"batch finished with {failures} run(s) exiting non-zero; see {manifest}")
    if missing:
        print(f"{len(missing)} run(s) produced no prediction row:")
        for iid, arm in missing:
            print(f"  {arm} {iid}")
    return 1 if missing else 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--instances",
        required=True,
        help="Instance JSON file, JSONL file, or directory of JSON files",
    )
    ap.add_argument(
        "--arm",
        action="append",
        choices=sorted(ARM_MODULES),
        help="Arm to run; repeat for more than one. Defaults to single and team.",
    )
    ap.add_argument("--out-dir", required=True, help="Directory for predictions, logs, manifest")
    ap.add_argument("--team-config", default=None, help="Team YAML, required for the team arm")
    ap.add_argument("--image", default=None, help="Image for instances that do not name one")
    ap.add_argument(
        "--budget-per-seat",
        type=int,
        default=DEFAULT_BUDGET,
        help="Tokens one agent may spend. A team is started with this once "
             "per declared role, so a seat is worth the same on every arm.",
    )
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--limit", type=int, default=None, help="Run only the first N instances")
    ap.add_argument("--dry-run", action="store_true", help="Print the commands and stop")
    ap.add_argument(
        "--pass-through",
        nargs=argparse.REMAINDER,
        default=[],
        help="Everything after this flag is appended to each generator command",
    )
    args = ap.parse_args(argv)
    if not args.arm:
        args.arm = ["single", "team"]
    if any(arm in TEAM_ARMS for arm in args.arm) and not args.team_config:
        ap.error("--team-config is required when the team arm is selected")
    return run_batch(args)


if __name__ == "__main__":
    raise SystemExit(main())
