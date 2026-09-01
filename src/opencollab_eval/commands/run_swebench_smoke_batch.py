#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from opencollab_eval.commands import swebench_process as process_tools
from opencollab_eval.commands import swebench_smoke_io as smoke_io
from opencollab_eval.commands.swebench_smoke_spec import make_test_spec
from opencollab_eval.engine.swe_eval_records import (
    MAX_JSONL_LINE_BYTES,
    MAX_JSONL_RETAINED_BYTES,
    MAX_JSONL_RETAINED_ROWS,
    MAX_JSONL_SCAN_BYTES,
)
from opencollab_eval.safe_files import (
    ensure_directory_no_symlinks,
)

REPO_ROOT = Path(os.environ.get("OPENCOLLAB_EVAL_WORKSPACE", Path.cwd())).resolve()

positive_timeout_seconds = process_tools.positive_timeout_seconds
_ensure_process_tree_quiesced_after_wait = process_tools.ensure_process_tree_quiesced_after_wait
_process_group_kwargs = process_tools.process_group_kwargs
_terminate_process_tree = process_tools.terminate_process_tree

PROCESS_TERM_GRACE_SECONDS = 0.1
PROCESS_KILL_REAP_SECONDS = 2.0
PROCESS_CLEANUP_SLACK_SECONDS = 0.5
PROCESS_SPAWN_TIMEOUT_SECONDS = 10.0
TECHNICAL_EXIT_CODE = 125
MAX_INSTANCE_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
SAFE_FILE_OPEN_RETRIES = 8
MAX_INSTANCE_DIRECTORY_ENTRIES = 10_000
HARNESS_LOCK_TIMEOUT_SECONDS = 10.0


def _generator_worker(
    *,
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    deadline: float,
    stop: threading.Event,
    started: threading.Event,
    done: threading.Event,
    state: dict,
    term_timeout: float,
    kill_timeout: float,
) -> None:
    process: subprocess.Popen | None = None
    try:
        try:
            process = subprocess.Popen(cmd, cwd=cwd, env=env, **_process_group_kwargs())
        except BaseException as exc:
            state.update(status="spawn_error", error=exc)
            return
        finally:
            started.set()

        while True:
            if stop.is_set():
                state.update(
                    status="interrupted",
                    cleanup_ok=_terminate_process_tree(
                        process,
                        term_timeout=term_timeout,
                        kill_timeout=kill_timeout,
                    ),
                )
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                state.update(
                    status="timeout",
                    cleanup_ok=_terminate_process_tree(
                        process,
                        term_timeout=term_timeout,
                        kill_timeout=kill_timeout,
                    ),
                )
                return
            try:
                returncode = process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                continue
            state.update(
                status="completed",
                returncode=returncode,
                cleanup_ok=_ensure_process_tree_quiesced_after_wait(
                    process,
                    term_timeout=term_timeout,
                    kill_timeout=kill_timeout,
                ),
            )
            return
    except BaseException as exc:
        state.update(status="worker_error", error=exc)
        if process is not None:
            state["cleanup_ok"] = _terminate_process_tree(
                process,
                term_timeout=term_timeout,
                kill_timeout=kill_timeout,
            )
    finally:
        done.set()


def _wait_event_resisting_interrupt(
    event: threading.Event,
    *,
    timeout: float,
    interrupt_event: threading.Event | None = None,
) -> tuple[bool, BaseException | None]:
    deadline = time.monotonic() + max(0.0, timeout)
    interruption: BaseException | None = None
    while not event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            event.wait(min(0.05, remaining))
        except (KeyboardInterrupt, SystemExit) as exc:
            if interruption is None:
                interruption = exc
                if interrupt_event is not None:
                    interrupt_event.set()
    return event.is_set(), interruption


def _install_termination_handlers(
    stop: threading.Event,
    previous: dict[int, object],
) -> None:
    if threading.current_thread() is not threading.main_thread():
        return

    def request_stop(signum, _frame):
        stop.set()
        raise SystemExit(128 + signum)

    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)


def _restore_termination_handlers(previous: dict[int, object]) -> None:
    if threading.current_thread() is not threading.main_thread():
        return
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _run_generator_thread(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    outer_timeout: float,
    spawn_timeout: float = PROCESS_SPAWN_TIMEOUT_SECONDS,
    term_timeout: float = PROCESS_TERM_GRACE_SECONDS,
    kill_timeout: float = PROCESS_KILL_REAP_SECONDS,
) -> tuple[int, str]:
    stop = threading.Event()
    started = threading.Event()
    done = threading.Event()
    state: dict = {}
    previous_handlers: dict[int, object] = {}
    worker = threading.Thread(
        target=_generator_worker,
        kwargs={
            "cmd": cmd,
            "cwd": cwd,
            "env": env,
            "deadline": time.monotonic() + outer_timeout,
            "stop": stop,
            "started": started,
            "done": done,
            "state": state,
            "term_timeout": term_timeout,
            "kill_timeout": kill_timeout,
        },
        name="swe-smoke-generator",
        # ``Popen`` has no constructor-level timeout.  Keep the supervisor
        # daemonized so an OS-level launch stall cannot hold interpreter
        # shutdown forever after the bounded spawn wait expires.  Once the
        # child is created, the worker owns normal process-group teardown.
        daemon=True,
    )
    try:
        _install_termination_handlers(stop, previous_handlers)
        worker.start()

        spawn_finished, interruption = _wait_event_resisting_interrupt(
            started,
            timeout=min(spawn_timeout, outer_timeout),
            interrupt_event=stop,
        )
        if not spawn_finished:
            stop.set()
            if interruption is not None:
                raise interruption
            return TECHNICAL_EXIT_CODE, "generator spawn exceeded its outer bound"
        if interruption is not None:
            raise interruption

        completed, interruption = _wait_event_resisting_interrupt(
            done,
            timeout=(
                outer_timeout
                + term_timeout
                + kill_timeout
                + PROCESS_CLEANUP_SLACK_SECONDS
            ),
            interrupt_event=stop,
        )
        if interruption is not None:
            raise interruption
        if not completed:
            stop.set()
            return TECHNICAL_EXIT_CODE, "generator cleanup exceeded its outer bound"

        status = state.get("status")
        if status == "completed":
            if not state.get("cleanup_ok"):
                return (
                    TECHNICAL_EXIT_CODE,
                    "generator leader exited but its process tree did not quiesce",
                )
            return int(state.get("returncode", TECHNICAL_EXIT_CODE)), ""
        if status == "timeout":
            if state.get("cleanup_ok"):
                return 124, f"generator exceeded outer timeout of {outer_timeout:g}s"
            return (
                TECHNICAL_EXIT_CODE,
                "generator timed out and its process tree did not quiesce",
            )
        if status == "spawn_error":
            return 127, f"generator failed to start: {state.get('error')}"
        return (
            TECHNICAL_EXIT_CODE,
            f"generator lifecycle failed: {state.get('error') or status}",
        )
    except (KeyboardInterrupt, SystemExit):
        stop.set()
        _wait_event_resisting_interrupt(
            done,
            timeout=term_timeout + kill_timeout + PROCESS_CLEANUP_SLACK_SECONDS,
        )
        raise
    finally:
        _restore_termination_handlers(previous_handlers)


def _run_generator_helper(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    outer_timeout: float,
    spawn_timeout: float,
    term_timeout: float,
    kill_timeout: float,
) -> tuple[int, str]:
    """Compatibility entry point for callers of the former helper."""
    return _run_generator_thread(
        cmd,
        cwd=cwd,
        env=env,
        outer_timeout=outer_timeout,
        spawn_timeout=spawn_timeout,
        term_timeout=term_timeout,
        kill_timeout=kill_timeout,
    )

def _run_generator(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    outer_timeout: float,
    spawn_timeout: float = PROCESS_SPAWN_TIMEOUT_SECONDS,
    term_timeout: float = PROCESS_TERM_GRACE_SECONDS,
    kill_timeout: float = PROCESS_KILL_REAP_SECONDS,
) -> tuple[int, str]:
    # Always use the Popen-backed supervisor.  A process-launch choice based on
    # a racy active-thread count could let a newly started thread's interpreter
    # lock state leak into the child.  Popen performs the minimal C-level spawn
    # setup and the worker thread owns all process-group cleanup.
    return _run_generator_thread(
        cmd,
        cwd=cwd,
        env=env,
        outer_timeout=outer_timeout,
        spawn_timeout=spawn_timeout,
        term_timeout=term_timeout,
        kill_timeout=kill_timeout,
    )


def _read_instance(path: Path) -> dict:
    return smoke_io.read_instance(path, max_bytes=MAX_INSTANCE_BYTES)


def _read_prediction_rows(path: Path) -> list[dict]:
    return smoke_io.read_prediction_rows(
        path,
        scan_bytes=MAX_JSONL_SCAN_BYTES,
        line_bytes=MAX_JSONL_LINE_BYTES,
        retained_rows=MAX_JSONL_RETAINED_ROWS,
        retained_bytes=MAX_JSONL_RETAINED_BYTES,
    )


def _prediction_has_patch(output: Path, instance_id: str) -> bool:
    return smoke_io.prediction_has_patch(
        output,
        instance_id,
        read_rows=_read_prediction_rows,
    )


_fsync_directory = smoke_io.fsync_directory


def _append_manifest_record(path: Path, record: dict) -> None:
    smoke_io.append_manifest_record(
        path,
        record,
        max_bytes=MAX_MANIFEST_BYTES,
        retries=SAFE_FILE_OPEN_RETRIES,
        lock_timeout=HARNESS_LOCK_TIMEOUT_SECONDS,
        sync_directory=_fsync_directory,
    )


def _discover_instance_paths(instances_dir: Path, *, limit: int) -> list[Path]:
    return smoke_io.discover_instance_paths(
        instances_dir,
        limit=limit,
        max_entries=MAX_INSTANCE_DIRECTORY_ENTRIES,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a small OpenCollab SWE-bench smoke batch")
    parser.add_argument("--instances-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--budget", type=int, default=1_000_000)
    parser.add_argument("--max-steps", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument(
        "--outer-timeout",
        type=float,
        default=None,
        help="Per-instance wall bound; defaults to --timeout plus 120 seconds.",
    )
    parser.add_argument(
        "--spawn-timeout",
        type=float,
        default=PROCESS_SPAWN_TIMEOUT_SECONDS,
    )
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--arch", default="x86_64")
    args = parser.parse_args()

    if args.limit <= 0:
        parser.error("--limit must be positive")
    if args.budget <= 0:
        parser.error("--budget must be positive")
    if args.max_steps <= 0:
        parser.error("--max-steps must be positive")
    try:
        args.timeout = positive_timeout_seconds(args.timeout, name="--timeout")
        args.spawn_timeout = positive_timeout_seconds(
            args.spawn_timeout,
            name="--spawn-timeout",
        )
        outer_timeout = positive_timeout_seconds(
            args.outer_timeout if args.outer_timeout is not None else args.timeout + 120.0,
            name="--outer-timeout",
        )
    except ValueError as exc:
        parser.error(str(exc))
    instances_dir = Path(os.path.abspath(args.instances_dir))
    output_dir = Path(os.path.abspath(args.output_dir))
    try:
        ensure_directory_no_symlinks(output_dir)
    except OSError as exc:
        parser.error(f"unsafe output directory: {exc}")

    output_path = output_dir / "predictions.jsonl"
    manifest_path = output_dir / "manifest.jsonl"
    try:
        instance_paths = _discover_instance_paths(instances_dir, limit=args.limit)
    except ValueError as exc:
        parser.error(str(exc))
    if not instance_paths:
        raise SystemExit(f"No instance JSON files found in {instances_dir}")

    env = os.environ.copy()
    default_cache_root = output_dir / ".cache"
    default_cache_paths = {
        "TMPDIR": default_cache_root / "tmp",
        "HF_HOME": default_cache_root / "hf",
        "HF_DATASETS_CACHE": default_cache_root / "datasets",
    }
    for key, path in default_cache_paths.items():
        configured = Path(env.get(key) or path)
        configured = Path(os.path.abspath(os.fspath(configured)))
        try:
            ensure_directory_no_symlinks(configured)
        except OSError:
            # Environment-provided cache aliases commonly traverse system
            # symlinks (for example /var on macOS).  Redirect them into the
            # owned output tree so every writable cache parent remains bound
            # to a verified lexical directory chain.
            configured = Path(os.path.abspath(os.fspath(path)))
            try:
                ensure_directory_no_symlinks(configured)
            except OSError as exc:
                parser.error(f"unsafe {key} directory: {exc}")
        env[key] = str(configured)
    arch_config = {
        "x86_64": ("x86_64", "linux/amd64"),
        "amd64": ("x86_64", "linux/amd64"),
        "arm64": ("arm64", "linux/arm64"),
        "aarch64": ("arm64", "linux/arm64"),
    }.get(args.arch)
    if arch_config is None:
        parser.error("--arch must be one of: x86_64, amd64, arm64, aarch64")
    spec_arch, docker_platform = arch_config
    env["DOCKER_DEFAULT_PLATFORM"] = docker_platform
    env.setdefault("OPENCOLLAB_DOCKER_TIMEOUT", "900")
    env.setdefault("OPENCOLLAB_TEMPERATURE", "0.2")
    env.setdefault("OPENCOLLAB_THINKING", "false")
    env.setdefault("OPENCOLLAB_LLM_TIMEOUT", "240")
    failures: list[str] = []

    for path in instance_paths:
        instance = _read_instance(path)
        instance_id = instance["instance_id"]
        spec = make_test_spec(instance, namespace="swebench", arch=spec_arch)
        image = spec.instance_image_key
        print(f"\n=== {instance_id} ===", flush=True)
        print(f"image: {image}", flush=True)

        if _prediction_has_patch(output_path, instance_id):
            print("prediction with patch already exists, skipping", flush=True)
            continue

        record = {
            "instance_id": instance_id,
            "instance_file": str(path),
            "image": image,
            "model_name": args.model_name,
        }
        _append_manifest_record(manifest_path, record)

        cmd = [
            sys.executable,
            "-m",
            "opencollab_eval.generation.gen_prediction",
            "--instance-file",
            str(path),
            "--output",
            str(output_path),
            "--metrics",
            str(output_dir / "metrics.jsonl"),
            "--image",
            image,
            "--model-name",
            args.model_name,
            "--budget",
            str(args.budget),
            "--max-steps",
            str(args.max_steps),
            "--timeout",
            str(args.timeout),
        ]
        returncode, lifecycle_reason = _run_generator(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            outer_timeout=outer_timeout,
            spawn_timeout=args.spawn_timeout,
        )
        if returncode != 0:
            detail = f" ({lifecycle_reason})" if lifecycle_reason else ""
            print(
                f"instance failed with exit code {returncode}: {instance_id}{detail}",
                flush=True,
            )
            failures.append(instance_id)
        elif not _prediction_has_patch(output_path, instance_id):
            print(f"instance produced no non-empty patch: {instance_id}", flush=True)
            failures.append(instance_id)

    print(f"\nBatch output: {output_path}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
