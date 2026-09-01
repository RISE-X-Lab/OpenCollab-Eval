"""Run every arm over a list of instances.

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
* **How many run at once is the caller's decision, and one is the default.**
  A task container is heavy and the machine running it is usually shared, so
  the driver does not help itself to it. ``--concurrency`` raises the number in
  flight; nothing else changes, because everything a run touches is already its
  own -- its log directory, its container name, and a predictions file every
  generator appends to under an exclusive lock. Jobs are submitted in the
  planned order, so at a concurrency of three whole tasks are in flight rather
  than one arm of three different tasks.
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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    "self-collaboration": "opencollab_eval.generation.gen_prediction_workflow",
    "self-collaboration-reading-analyst": (
        "opencollab_eval.generation.gen_prediction_workflow"
    ),
}

#: The generator that has to be *told* which solver to open.
#:
#: An arm that runs through it and is listed in neither table below is started
#: with no ``--workflow`` and no ``--team-config``, and that generator's last
#: branch falls back to the built-in ``generate_review_fix``. The run happens,
#: exits zero, and appends a row to ``preds-<arm>.jsonl`` that is a different
#: arm's run; nothing in the output says so, and the arm it was meant to be is
#: never run. That is the reason the wiring below is checked rather than
#: remembered.
ORCHESTRATED_GENERATOR = "opencollab_eval.generation.gen_prediction_workflow"

#: Arms whose generator is the workflow entry point and therefore takes
#: ``--team-config``. Kept separate from ``ARM_MODULES`` so adding a workflow
#: arm that is not the team does not silently inherit the team's configuration.
TEAM_ARMS: frozenset[str] = frozenset({"team"})

#: Arms that select a bundled workflow by name instead of a team file. They
#: share the team's generator and none of its configuration: a workflow is
#: sequenced by its own code, so what it needs is ``--workflow``.
WORKFLOW_ARMS: dict[str, str] = {
    "self-collaboration": "self-collaboration",
    "self-collaboration-reading-analyst": "self-collaboration-reading-analyst",
}

MANIFEST_NAME = "manifest.jsonl"


def validate_arm_wiring(
    arm_modules: dict[str, str] | None = None,
    team_arms: frozenset[str] | None = None,
    workflow_arms: dict[str, str] | None = None,
) -> None:
    """Every arm names a solver, and only the arms that need one name it.

    Run at import, so a table edited in one place and not the other cannot be
    discovered by a batch. The three tables are separate on purpose -- an arm's
    module, its team file, its workflow name are different decisions -- and the
    cost of that is that they can disagree. Every way they can disagree is a
    silent one:

    * an orchestrated arm in neither table is run as ``generate_review_fix``;
    * an arm in both tables would be given a workflow *and* a team file, which
      the generator's own mutually exclusive group rejects only at run time,
      after a container is up;
    * a name in one of the tables that is not an arm is a rename that was done
      halfway, and the arm it used to configure is now configured by nothing.
    """
    arm_modules = ARM_MODULES if arm_modules is None else arm_modules
    team_arms = TEAM_ARMS if team_arms is None else team_arms
    workflow_arms = WORKFLOW_ARMS if workflow_arms is None else workflow_arms

    both = sorted(set(team_arms) & set(workflow_arms))
    if both:
        raise ValueError(f"arms configured as both team and workflow: {both}")
    unknown = sorted((set(team_arms) | set(workflow_arms)) - set(arm_modules))
    if unknown:
        raise ValueError(f"solver configuration for arms the driver cannot run: {unknown}")
    for arm, module in sorted(arm_modules.items()):
        selected = arm in team_arms or arm in workflow_arms
        if module == ORCHESTRATED_GENERATOR and not selected:
            raise ValueError(
                f"arm {arm!r} runs through {ORCHESTRATED_GENERATOR} and names no "
                "solver, so it would silently run the built-in review-fix "
                "workflow; add it to TEAM_ARMS or WORKFLOW_ARMS"
            )
        if module != ORCHESTRATED_GENERATOR and selected:
            raise ValueError(
                f"arm {arm!r} names a solver but does not run through "
                f"{ORCHESTRATED_GENERATOR}, which is the only generator that "
                "reads one"
            )


validate_arm_wiring()


def _arm_module(arm: str) -> str:
    """The module that runs ``arm``, or a refusal that names the arms there are.

    ``pool_for`` used to answer for an unregistered arm by falling through to
    the single-seat budget, which is a plausible number and the wrong one.
    """
    try:
        return ARM_MODULES[arm]
    except KeyError:
        raise ValueError(
            f"unknown arm {arm!r}; the driver runs {sorted(ARM_MODULES)}"
        ) from None

#: The sampling temperature every arm's generator process is started with.
#:
#: Pinned here rather than by changing OpenCollab's ``DEFAULT_TEMPERATURE``,
#: which is a framework-wide default every other user of that library gets.
#:
#: 1.0 because of what the Best-of-N arm is: N independent samples of one
#: seat, from which a fixed selector keeps one. At a temperature low enough to
#: make the model near-deterministic those N candidates are one candidate drawn
#: N times, and the arm would be measuring nothing but its own selector. The
#: other arms are given the same value because a sampling difference between
#: arms would sit exactly on the axis they are supposed to be equal on.
ARM_SAMPLING_TEMPERATURE = "1.0"


def generator_environment(log_dir: Path) -> dict[str, str]:
    """The environment one ``(instance, arm)`` generator process is started with.

    A function rather than a literal at the call site because it is also the
    honest answer to "what temperature does a run use?": the value reaches the
    generator through the environment, so a check that reads this process's own
    ``OPENCOLLAB_TEMPERATURE`` is reading the wrong machine's setting. The
    cross-arm alignment audit resolves the configuration under exactly this
    mapping.
    """
    return {
        **os.environ,
        "OPENCOLLAB_EVAL_WORKFLOW_LOG_DIR": str(log_dir),
        "OPENCOLLAB_TEMPERATURE": ARM_SAMPLING_TEMPERATURE,
    }


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


def workflow_seats(workflow_name: str) -> int:
    """How many seats a bundled workflow divides its pool between.

    Read off the workflow's own module rather than tabulated here, so a
    workflow that changes how many roles it seats cannot end up funded for a
    different number than it caps. A workflow that declares nothing seats one.
    """
    from opencollab_eval.generation.gen_prediction_workflow import _BUNDLED_WORKFLOWS

    function = _BUNDLED_WORKFLOWS.get(workflow_name)
    if function is None:
        raise ValueError(f"unknown workflow {workflow_name!r}")
    module = sys.modules.get(getattr(function, "__module__", ""))
    seats = getattr(module, "SEATS", 1)
    if not isinstance(seats, int) or isinstance(seats, bool) or seats < 1:
        raise ValueError(f"workflow {workflow_name!r} declares an unusable seat count")
    return seats


def pool_for(arm: str, budget_per_seat: int, team_config: Path | None) -> int:
    """The token pool one run of ``arm`` is started with.

    One seat, one solo agent's budget. A single-agent run has one seat and is
    given the figure itself; a team is given it once per role its file
    declares, because the scheduler divides the pool by that same count, and a
    workflow arm once per seat its own module declares, for the same reason.
    """
    _arm_module(arm)
    if arm in WORKFLOW_ARMS:
        return budget_per_seat * workflow_seats(WORKFLOW_ARMS[arm])
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
    module = _arm_module(arm)
    command = [
        sys.executable,
        "-m",
        module,
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
    if arm in WORKFLOW_ARMS:
        command += ["--workflow", WORKFLOW_ARMS[arm]]
    elif arm in TEAM_ARMS:
        if team_config is None:
            raise ValueError(f"arm {arm!r} needs --team-config")
        command += ["--team-config", str(team_config)]
    # The argv is the last place this can still be caught, and the only one
    # that sees what was actually built: an arm added to ARM_MODULES on the
    # orchestrated generator and to neither solver table produces a command
    # with no solver flag, which that generator answers by running the built-in
    # review-fix workflow under this arm's name.
    if (module == ORCHESTRATED_GENERATOR) != bool(
        {"--workflow", "--team-config"} & set(command)
    ):
        raise ValueError(
            f"arm {arm!r} would be run as {' '.join(command[2:])!r}, which does "
            "not name the solver this arm is; see validate_arm_wiring"
        )
    command += list(extra)
    return command


def _instance_path(instance: dict[str, Any], staging: Path) -> Path:
    """Write the instance out so the generator reads exactly what we selected.

    Written to a temporary name and renamed into place. Two arms of the same
    task are usually two processes sharing this directory, they stage the same
    instance under the same name, and an instance record is tens of kilobytes:
    a plain write truncates the file first, so the other arm's generator can
    open it mid-write and read a record that ends in the middle of the problem
    statement. A rename is atomic, and a reader already holding the old file
    keeps reading it to the end.
    """
    staging.mkdir(parents=True, exist_ok=True)
    path = staging / f"{instance['instance_id']}.json"
    staged = staging / f".{instance['instance_id']}.{os.getpid()}.tmp"
    staged.write_text(json.dumps(instance), encoding="utf-8")
    staged.replace(path)
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


def _run_one(
    *,
    command: Sequence[str],
    log_dir: Path,
) -> tuple[int, float]:
    """Run one (instance, arm) and return its return code and wall time.

    Everything a run touches is already per-run: its own log directory, its own
    container name, and a predictions file each generator appends to under an
    exclusive lock. That is what makes running several at once a scheduling
    decision rather than a change to what any run does.
    """
    started = time.monotonic()
    with (log_dir / "driver.log").open("wb") as sink:
        completed = subprocess.run(
            command,
            stdout=sink,
            stderr=subprocess.STDOUT,
            env=generator_environment(log_dir),
            check=False,
        )
    return completed.returncode, round(time.monotonic() - started, 1)


def run_batch(args: argparse.Namespace) -> int:
    concurrency = getattr(args, "concurrency", 1)
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or concurrency < 1
    ):
        raise ValueError("concurrency must be a positive integer")
    args.concurrency = concurrency
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

    # Prepared in the planned order whether or not they are run in it: with a
    # concurrency of N the first N jobs are submitted first, and because the
    # order is instance-major that means whole tasks are in flight rather than
    # one arm of many tasks. A batch stopped halfway still has complete sets.
    jobs = []
    for index, (instance, arm) in enumerate(work, start=1):
        iid = instance["instance_id"]
        log_dir = out_dir / f"logs-{arm}" / iid
        log_dir.mkdir(parents=True, exist_ok=True)
        command = build_command(
            arm=arm,
            instance_path=_instance_path(instance, out_dir / "instances"),
            predictions=predictions[arm],
            team_config=Path(args.team_config) if args.team_config else None,
            budget_per_seat=args.budget_per_seat,
            max_steps=args.max_steps,
            timeout=args.timeout,
            image=instance.get("image") or args.image,
            extra=args.pass_through,
        )
        jobs.append((index, iid, arm, log_dir, command))

    if args.dry_run:
        for index, iid, arm, _log_dir, command in jobs:
            print(f"[{index}/{len(work)}] {arm} {iid}")
            print("  " + " ".join(command))
        return 0

    def record(index, iid, arm, log_dir, command, returncode, elapsed) -> None:
        nonlocal failures
        failures += 0 if returncode == 0 else 1
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "instance_id": iid,
                        "arm": arm,
                        "returncode": returncode,
                        "seconds": elapsed,
                        "finished_at": _now(),
                        "log_dir": str(log_dir),
                        "command": command,
                    }
                )
                + "\n"
            )
        print(f"[{index}/{len(work)}] {arm} {iid} rc={returncode} in {elapsed}s", flush=True)

    if args.concurrency == 1:
        for index, iid, arm, log_dir, command in jobs:
            print(f"[{index}/{len(work)}] {arm} {iid}", flush=True)
            returncode, elapsed = _run_one(command=command, log_dir=log_dir)
            record(index, iid, arm, log_dir, command, returncode, elapsed)
    else:
        print(f"running {args.concurrency} at a time", flush=True)
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(_run_one, command=job[4], log_dir=job[3]): job
                for job in jobs
            }
            for future in as_completed(futures):
                index, iid, arm, log_dir, command = futures[future]
                returncode, elapsed = future.result()
                record(index, iid, arm, log_dir, command, returncode, elapsed)

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
    ap.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help=(
            "How many runs to have in flight at once. One by default: the task "
            "containers are heavy and the machine is usually shared, so this is "
            "a decision about somebody's machine and is not made for them."
        ),
    )
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
