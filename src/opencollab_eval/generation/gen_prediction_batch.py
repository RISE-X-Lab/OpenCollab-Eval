"""Run every arm over a list of instances.

Arms are interleaved within each instance so interrupted batches retain
complete comparisons. Jobs are submitted in that order, with concurrency
controlled by the caller and defaulting to one.

A task failure ends only that instance-arm run. The exit status counts missing
predictions because controlled stops can exit nonzero after writing a patch.
Resumption skips predictions already present in each arm's output file.

Budgets are per seat. Team and workflow pools scale with their declared role
counts. Best-of-N shares one seat's pool across its candidates.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
import uuid
from collections.abc import Iterable, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opencollab.teams import declared_role_names

from opencollab_eval.engine.swe_eval_records import embedded_workflow_metric
from opencollab_eval.safe_files import open_regular_text_append

from .batch_processes import BatchStop, run_generator
from .gen_prediction_constants import (
    DEFAULT_BUDGET,
    DEFAULT_MAX_STEPS,
    DEFAULT_TIMEOUT,
    MAX_INSTANCE_BYTES,
)
from .gen_prediction_safe_output import _append_jsonl_durable, default_metrics_path, output_paths

ARM_MODULES: dict[str, str] = {
    "single": "opencollab_eval.generation.gen_prediction",
    "best-of-n": "opencollab_eval.generation.gen_prediction_best_of_n",
    "team": "opencollab_eval.generation.gen_prediction_workflow",
    "self-collaboration": "opencollab_eval.generation.gen_prediction_workflow",
    "self-collaboration-reading-analyst": (
        "opencollab_eval.generation.gen_prediction_workflow"
    ),
}

#: An orchestrated arm without a solver flag silently runs generate_review_fix.
#: Validate the solver tables before launching a batch.
ORCHESTRATED_GENERATOR = "opencollab_eval.generation.gen_prediction_workflow"

#: Workflow-entry arms that require a team file.
TEAM_ARMS: frozenset[str] = frozenset({"team"})

#: Workflow-entry arms that select a bundled workflow by name.
WORKFLOW_ARMS: dict[str, str] = {
    "self-collaboration": "self-collaboration",
    "self-collaboration-reading-analyst": "self-collaboration-reading-analyst",
}

#: Reporting includes scripted workflow roles. TEAM_ARMS also controls spec
#: requirements for team cells and rungs, so it remains the narrower set.
DELIVERY_READABLE_ARMS: frozenset[str] = TEAM_ARMS | frozenset(WORKFLOW_ARMS)

MANIFEST_NAME = "manifest.jsonl"


def validate_arm_wiring(
    arm_modules: dict[str, str] | None = None,
    team_arms: frozenset[str] | None = None,
    workflow_arms: dict[str, str] | None = None,
) -> None:
    """Reject inconsistent generator, team, and workflow tables before launch.

    An orchestrated arm without a solver silently falls back to review-fix.
    Each orchestrated arm must therefore appear in exactly one solver table,
    and every solver-table name must refer to a registered orchestrated arm.
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
    """Return the registered generator module or reject the arm."""
    try:
        return ARM_MODULES[arm]
    except KeyError:
        raise ValueError(
            f"unknown arm {arm!r}; the driver runs {sorted(ARM_MODULES)}"
        ) from None

#: Temperature 1.0 keeps Best-of-N samples independent. Apply the same value
#: to every arm for comparability without changing the framework default.
ARM_SAMPLING_TEMPERATURE = "1.0"


def generator_environment(log_dir: Path, run_id: str | None = None) -> dict[str, str]:
    """Build the environment passed to one instance-arm generator process.

    Configuration audits must inspect this mapping because its temperature
    overrides the parent process environment.
    """
    environment = {
        **os.environ,
        "OPENCOLLAB_EVAL_WORKFLOW_LOG_DIR": str(log_dir),
        "OPENCOLLAB_TEMPERATURE": ARM_SAMPLING_TEMPERATURE,
    }
    if run_id is not None:
        # Join the manifest, trajectory, and metrics on the same run id.
        environment["OPENCOLLAB_RUN_ID"] = run_id
    return environment


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_instances(source: Path) -> list[dict[str, Any]]:
    """Read JSON files in sorted filename order or JSONL records in file order."""
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


def _prediction_records(predictions: Path) -> Iterable[dict[str, Any]]:
    """Read durable prediction rows while tolerating interrupted JSONL lines."""
    if not predictions.is_file():
        return
    with predictions.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def completed_instance_ids(predictions: Path) -> set[str]:
    """The instance ids a predictions file already holds."""
    done: set[str] = set()
    for record in _prediction_records(predictions):
        iid = record.get("instance_id")
        if isinstance(iid, str):
            done.add(iid)
    return done


def recover_metric_projections(predictions: Path) -> None:
    """Complete missing metric appends from committed prediction records.

    Every attempt keeps its own metric. The durable append handles an already
    present projection and refuses a conflicting row under the metrics lock.
    """
    predictions, metrics = output_paths(predictions, default_metrics_path(predictions))
    for prediction in _prediction_records(predictions):
        metric = embedded_workflow_metric(prediction)
        if metric is None:
            continue
        instance_id = prediction.get("instance_id")
        record_id = prediction.get("record_id")
        if (
            not isinstance(instance_id, str)
            or not instance_id
            or not isinstance(record_id, str)
            or not record_id
            or metric.get("instance_id") != instance_id
            or metric.get("record_id") != record_id
        ):
            continue
        _append_jsonl_durable(metrics, metric)


def workflow_seats(workflow_name: str) -> int:
    """Read the workflow's declared SEATS, defaulting to one.

    Using the workflow's own declaration keeps pool allocation aligned with caps.
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


def best_of_n_candidates() -> int:
    """Read the candidate count from the Best-of-N generator."""
    from opencollab_eval.generation import gen_prediction_best_of_n

    candidates = gen_prediction_best_of_n.CANDIDATES
    if not isinstance(candidates, int) or isinstance(candidates, bool) or candidates < 1:
        raise ValueError("the Best-of-N arm declares an unusable candidate count")
    return candidates


def pool_for(arm: str, budget_per_seat: int | None, team_config: Path | None) -> int | None:
    """Allocate one solo-agent budget per declared team or workflow seat.

    Best-of-N shares one seat's pool across N candidates, so its pool remains
    the single-agent budget regardless of candidate count.
    """
    _arm_module(arm)
    if budget_per_seat is None:
        return None
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
    budget_per_seat: int | None,
    max_steps: int | None,
    timeout: float,
    image: str | None,
    extra: Sequence[str] = (),
) -> list[str]:
    """Build the argv for one instance-arm run.

    Pass an explicit instance image because published swebench/ names can differ
    from the generators' derived local image names.
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
        "--timeout",
        str(timeout),
    ]
    pool = pool_for(arm, budget_per_seat, team_config)
    if pool is not None:
        command += ["--budget", str(pool)]
    if max_steps is not None:
        command += ["--max-steps", str(max_steps)]
    if image:
        command += ["--image", image]
    if arm in WORKFLOW_ARMS:
        command += ["--workflow", WORKFLOW_ARMS[arm]]
    elif arm in TEAM_ARMS:
        if team_config is None:
            raise ValueError(f"arm {arm!r} needs --team-config")
        command += ["--team-config", str(team_config)]
    # Catch missing solver flags in the built argv before the orchestrated
    # generator silently runs review-fix under another arm's name.
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
    """Atomically stage the selected instance for concurrent arm generators.

    Arms share the instance filename. A temporary write followed by rename keeps
    readers from observing a truncated record.
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
    """Order every arm of one instance before moving to the next instance."""
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
    stop: BatchStop,
    run_id: str | None = None,
) -> tuple[int | None, float]:
    """Run one instance-arm pair and return its exit code and wall time.

    Runs use separate log directories and container names. Prediction appends
    share an exclusive lock.
    """
    started = time.monotonic()
    with (log_dir / "driver.log").open("wb") as sink:
        returncode = run_generator(list(command), sink, generator_environment(log_dir, run_id), stop)
    return returncode, round(time.monotonic() - started, 1)


def run_batch(args: argparse.Namespace) -> int:
    """Allow one driver to dispatch work into an output directory at a time."""
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open_regular_text_append(out_dir / ".driver.lock") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"batch driver already running for {out_dir}", flush=True)
            return 0
        try:
            return _run_batch_locked(args)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _run_batch_locked(args: argparse.Namespace) -> int:
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
    if not args.dry_run:
        for path in predictions.values():
            recover_metric_projections(path)
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

    # Submit complete instance-major groups first so interrupted batches
    # retain complete comparisons.
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
        jobs.append((index, iid, arm, log_dir, command, f"{arm}-{uuid.uuid4().hex}"))

    if args.dry_run:
        for index, iid, arm, _log_dir, command, _run_id in jobs:
            print(f"[{index}/{len(work)}] {arm} {iid}")
            print("  " + " ".join(command))
        return 0

    def record(index, iid, arm, log_dir, command, run_id, returncode, elapsed) -> None:
        nonlocal failures
        failures += 0 if returncode == 0 else 1
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "instance_id": iid,
                        "arm": arm,
                        "run_id": run_id,
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

    with BatchStop() as stop:
        if args.concurrency == 1:
            for index, iid, arm, log_dir, command, run_id in jobs:
                if stop.event.is_set():
                    break
                print(f"[{index}/{len(work)}] {arm} {iid}", flush=True)
                returncode, elapsed = _run_one(command=command, log_dir=log_dir, stop=stop, run_id=run_id)
                if returncode is not None:
                    record(index, iid, arm, log_dir, command, run_id, returncode, elapsed)
        else:
            print(f"running {args.concurrency} at a time", flush=True)
            pool = ThreadPoolExecutor(max_workers=args.concurrency)
            futures = {}
            pending_jobs = iter(jobs)

            def submit_next() -> None:
                job = next(pending_jobs, None)
                if job is not None and not stop.event.is_set():
                    futures[pool.submit(_run_one, command=job[4], log_dir=job[3], stop=stop, run_id=job[5])] = job

            try:
                for _ in range(args.concurrency):
                    submit_next()
                while futures:
                    completed, _ = wait(futures, timeout=0.1, return_when=FIRST_COMPLETED)
                    for future in completed:
                        job = futures.pop(future)
                        returncode, elapsed = future.result()
                        if returncode is not None:
                            record(*job, returncode, elapsed)
                    if not stop.event.is_set():
                        for _ in completed:
                            submit_next()
            except BaseException:
                stop.request()
                raise
            finally:
                pool.shutdown(wait=True, cancel_futures=True)

        if stop.event.is_set():
            for path in predictions.values():
                recover_metric_projections(path)
            return 128 + stop.signum

    for path in predictions.values():
        recover_metric_projections(path)

    missing = [
        (instance["instance_id"], arm)
        for instance, arm in work
        if instance["instance_id"] not in completed_instance_ids(predictions[arm])
    ]
    # Controlled stops can write a prediction before exiting nonzero.
    # Missing predictions determine whether the batch needs a rerun.
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
