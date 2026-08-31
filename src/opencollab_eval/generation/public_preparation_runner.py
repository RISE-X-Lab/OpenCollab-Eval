"""Run trusted public repository setup and quiesce its process group."""

from __future__ import annotations

import ctypes
import errno
import math
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

_CLEANUP_SECONDS = 3.0
_PREPARATION_TIMEOUT_SECONDS = 900.0
_MAX_PREPARATION_TIMEOUT_SECONDS = 86_400.0
_PROCESS_SCAN_TIMEOUT_SECONDS = 1.0
_MAX_PROCESS_SCAN_OUTPUT_BYTES = 4 * 1024 * 1024
_PROCESS_SCAN_INTERVAL_SECONDS = 0.05
_PR_SET_CHILD_SUBREAPER = 36
_REAL_POPEN = subprocess.Popen


class ProcessInspectionError(RuntimeError):
    """The runner cannot prove ownership of every preparation descendant."""


class _DescendantSnapshot(set[int]):
    """A PID set carrying start identities captured by the same scan.

    The public/private helper historically returned a plain ``set[int]``.
    Keeping a set subclass preserves that contract while allowing cleanup to
    reject a PID that has been recycled after a leader exits.
    """

    def __init__(
        self, values: set[int] | None = None, identities: dict[int, str] | None = None
    ) -> None:
        super().__init__(values or ())
        self.identities = dict(identities or {})


def _proc_stat(pid: int) -> tuple[int, str, str] | None:
    """Read ``(parent, state, start-identity)`` for one Linux PID.

    ``ENOENT`` is a normal process-exit race.  Other read/parse failures are
    ownership-proof failures and must not be silently converted to an empty
    process tree.
    """
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProcessInspectionError(f"cannot inspect /proc/{pid}/stat") from exc
    close = raw.rfind(")")
    fields = raw[close + 2 :].split() if close >= 0 else []
    if len(fields) < 20:
        raise ProcessInspectionError(f"malformed /proc/{pid}/stat")
    try:
        parent = int(fields[1])
    except ValueError as exc:
        raise ProcessInspectionError(f"invalid parent in /proc/{pid}/stat") from exc
    state = fields[0]
    start = fields[19]
    if not state or not start:
        raise ProcessInspectionError(f"invalid identity in /proc/{pid}/stat")
    return parent, state, f"proc:{start}"


def _ps_process_state_identity(pid: int) -> tuple[str, str] | None:
    """Read one non-Linux PID's state and start time with bounded ``ps``."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "state=,lstart="],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=_PROCESS_SCAN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessInspectionError("bounded ps process inspection failed") from exc
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise ProcessInspectionError(
            f"ps process inspection exited with {result.returncode}"
        )
    rows = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if len(rows) != 1:
        raise ProcessInspectionError("ps process inspection returned malformed data")
    fields = rows[0].split(maxsplit=1)
    if len(fields) != 2 or not fields[0] or not fields[1].strip():
        raise ProcessInspectionError("ps process inspection returned malformed data")
    return fields[0], f"ps:{fields[1].strip()}"


def _process_state_identity(pid: int) -> tuple[str, str] | None:
    """Return a PID's state and stable-enough start identity."""
    if Path("/proc").is_dir():
        record = _proc_stat(pid)
        return None if record is None else (record[1], record[2])
    return _ps_process_state_identity(pid)


def _enable_subreaper() -> None:
    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _reap_children() -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _relations_descendants(
    parents: dict[int, tuple[int, str, str]], *, root_pid: int
) -> set[int]:
    family = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (parent, state, _identity) in parents.items():
            if not state.startswith("Z") and parent in family and pid not in family:
                family.add(pid)
                changed = True
    family.discard(root_pid)
    return family


def _ps_descendants(root_pid: int) -> set[int]:
    """Boundedly inspect parent relationships on hosts without ``/proc``."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,state=,lstart=,comm="],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=_PROCESS_SCAN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessInspectionError("bounded ps descendant inspection failed") from exc
    output = result.stdout or ""
    if result.returncode != 0:
        raise ProcessInspectionError(
            f"ps descendant inspection exited with {result.returncode}"
        )
    if len(output.encode("utf-8", errors="replace")) > _MAX_PROCESS_SCAN_OUTPUT_BYTES:
        raise ProcessInspectionError("ps descendant inspection exceeded its output bound")
    parents: dict[int, tuple[int, str, str]] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 3:
            raise ProcessInspectionError("ps descendant inspection returned malformed data")
        try:
            pid, parent = int(fields[0]), int(fields[1])
        except ValueError as exc:
            raise ProcessInspectionError(
                "ps descendant inspection returned malformed data"
            ) from exc
        if pid <= 0 or parent < 0:
            raise ProcessInspectionError("ps descendant inspection returned invalid pid")
        state = fields[2]
        if not state:
            raise ProcessInspectionError("ps descendant inspection returned empty state")
        # ``ps`` itself is a child of this runner and can appear in its own
        # process table.  Ignore that transient row; otherwise every scan
        # would discover a fresh descendant and cleanup would never converge.
        command = fields[8] if len(fields) >= 9 else (fields[3] if len(fields) >= 4 else "")
        if Path(command).name == "ps":
            continue
        # ``lstart`` is five whitespace-separated fields in the normal ps
        # format.  Keep a compact fallback for test/minimal BSD ps output; the
        # per-PID identity check below will replace it when available.
        if len(fields) >= 8:
            identity = "ps:" + " ".join(fields[3:8])
        else:
            identity = ""
        parents[pid] = (parent, state, identity)
    if root_pid not in parents:
        # The root is normally still present (the runner itself), but a
        # process table that cannot account for it is not a trustworthy empty
        # result.  This is especially important when ps is restricted by a
        # sandbox and silently omits other users' processes.
        raise ProcessInspectionError("ps descendant inspection omitted its root process")
    family = _relations_descendants(parents, root_pid=root_pid)
    identities = {
        pid: parents[pid][2]
        for pid in family
        if parents[pid][2] != "ps:"
    }
    return _DescendantSnapshot(family, identities)


def _descendants(root_pid: int | None = None) -> set[int]:
    root = os.getpid() if root_pid is None else root_pid
    proc = Path("/proc")
    if not proc.is_dir():
        return _ps_descendants(root)
    try:
        entries = list(proc.iterdir())
    except OSError as exc:
        raise ProcessInspectionError("cannot enumerate /proc") from exc
    parents: dict[int, tuple[int, str, str]] = {}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        record = _proc_stat(pid)
        if record is not None:
            parents[pid] = record
    if root not in parents:
        # An empty/incomplete /proc view is not evidence that the process tree
        # is empty.  Sandboxed hosts can hide the root or individual entries;
        # treating that omission as success would let detached descendants
        # survive into the next task.
        raise ProcessInspectionError("/proc descendant inspection omitted its root process")
    family = _relations_descendants(parents, root_pid=root)
    return _DescendantSnapshot(
        family,
        {pid: parents[pid][2] for pid in family},
    )


def _pid_is_live(pid: int) -> bool:
    permission_denied = False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Keep going: a readable process table can still distinguish a live
        # process from a zombie.  If that table is unavailable, the caller's
        # fail-closed inspection path will report a technical failure.
        permission_denied = True
    state_identity = _process_state_identity(pid)
    if state_identity is None:
        if permission_denied:
            raise ProcessInspectionError(f"cannot inspect inaccessible pid {pid}")
        return False
    return not state_identity[0].startswith("Z")


def _snapshot_identities(snapshot: set[int]) -> dict[int, str]:
    raw = getattr(snapshot, "identities", {})
    return {
        int(pid): identity
        for pid, identity in raw.items()
        if isinstance(pid, int) and isinstance(identity, str) and identity
    }


def _merge_observed(
    snapshot: set[int],
    observed: set[int],
    identities: dict[int, str],
    errors: list[BaseException] | None = None,
) -> None:
    """Retain only live PIDs with a start identity captured at observation."""
    snapshot_identities = _snapshot_identities(snapshot)
    for pid in snapshot:
        expected = snapshot_identities.get(pid)
        if expected is not None:
            observed.add(pid)
            identities[pid] = expected
            continue
        try:
            current = _process_state_identity(pid)
        except ProcessInspectionError as exc:
            if errors is not None:
                errors.append(exc)
            continue
        if current is None or current[0].startswith("Z"):
            continue
        observed.add(pid)
        identities[pid] = current[1]


def _pending_live(
    pending: set[int], identities: dict[int, str]
) -> set[int]:
    live: set[int] = set()
    for pid in pending:
        if not _pid_is_live(pid):
            continue
        expected = identities.get(pid)
        if expected is not None:
            current = _process_state_identity(pid)
            if current is None or current[0].startswith("Z") or current[1] != expected:
                continue
        live.add(pid)
    return live


def _quiesce_descendants(
    observed: set[int] | None = None,
    observed_identities: dict[int, str] | None = None,
) -> bool:
    pending = set(observed or ())
    identities = dict(observed_identities or {})
    try:
        for sent_signal in (signal.SIGTERM, signal.SIGKILL):
            descendants = _descendants()
            descendant_identities = _snapshot_identities(descendants)
            pending = _pending_live(pending, identities)
            targets = descendants | pending
            if not targets:
                _reap_children()
                return True
            for pid in targets:
                # A PID observed before leader exit may have been recycled.
                # Never signal it unless its start identity still matches the
                # observation.  Fresh descendants carry an identity from the
                # same process-table scan; a legacy/plain set is checked just
                # before signalling and remains fail-safe on inspection error.
                expected = identities.get(pid) or descendant_identities.get(pid)
                if expected is not None:
                    current = _process_state_identity(pid)
                    if (
                        current is None
                        or current[0].startswith("Z")
                        or current[1] != expected
                    ):
                        continue
                try:
                    os.kill(pid, sent_signal)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + _CLEANUP_SECONDS
            while time.monotonic() < deadline:
                _reap_children()
                descendants = _descendants()
                pending = _pending_live(pending, identities)
                if not descendants and not pending:
                    return True
                time.sleep(0.05)
        _reap_children()
        return not _descendants() and not _pending_live(pending, identities)
    except (OSError, ProcessInspectionError):
        # Process ownership is an all-or-nothing proof.  Never convert an
        # unavailable/failed inspection into an empty descendant set.
        return False


def _observe_descendants(
    process: subprocess.Popen,
    stop: threading.Event,
    observed: set[int],
    errors: list[BaseException],
    identities: dict[int, str] | None = None,
) -> None:
    """Sample a preparation tree before a non-Linux host reparents it."""
    while True:
        if process.poll() is not None:
            return
        try:
            snapshot = _descendants(process.pid)
            _merge_observed(
                snapshot,
                observed,
                identities if identities is not None else {},
                errors,
            )
        except (OSError, ProcessInspectionError) as exc:
            # A leader can exit between the poll above and the process-table
            # query; in that case its descendants have already been captured
            # by an earlier sample (or there are none to preserve), and the
            # missing root is expected rather than an inspection failure.
            if process.poll() is None:
                errors.append(exc)
            return
        if stop.wait(_PROCESS_SCAN_INTERVAL_SECONDS):
            return


def _group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise
    return True


def _quiesce_group(
    group_id: int,
    expected_leader_identity: str | None = None,
    *,
    leader_pid: int | None = None,
) -> bool:
    leader = group_id if leader_pid is None else leader_pid

    def leader_matches() -> bool:
        if expected_leader_identity is None:
            return True
        current = _process_state_identity(leader)
        # Once the original leader has exited, process-tree cleanup owns the
        # descendants.  Do not issue killpg against a potentially recycled
        # process-group id.
        return current is not None and not current[0].startswith("Z") and current[1] == expected_leader_identity

    if expected_leader_identity is not None:
        try:
            current = _process_state_identity(leader)
        except ProcessInspectionError:
            return False
        if current is None or current[0].startswith("Z") or current[1] != expected_leader_identity:
            return True
    if not _group_exists(group_id):
        return True
    try:
        if not leader_matches():
            return True
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + _CLEANUP_SECONDS
    while time.monotonic() < deadline:
        _reap_children()
        if expected_leader_identity is not None:
            try:
                if not leader_matches():
                    return True
            except ProcessInspectionError:
                return False
        if not _group_exists(group_id):
            return True
        time.sleep(0.05)
    try:
        if not leader_matches():
            return True
        os.killpg(group_id, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + _CLEANUP_SECONDS
    while time.monotonic() < deadline:
        _reap_children()
        if expected_leader_identity is not None:
            try:
                if not leader_matches():
                    return True
            except ProcessInspectionError:
                return False
        if not _group_exists(group_id):
            return True
        time.sleep(0.05)
    return not _group_exists(group_id)


_REAL_QUIESCE_GROUP = _quiesce_group


def _preparation_timeout_seconds() -> float:
    """Return a finite bound for untrusted setup commands.

    The setup script is data supplied by the evaluation input.  A hung
    dependency install must become a recorded technical failure rather than
    holding the official evaluator forever.
    """
    raw = os.environ.get(
        "OPENCOLLAB_PUBLIC_PREPARATION_TIMEOUT_SECONDS",
        str(_PREPARATION_TIMEOUT_SECONDS),
    ).strip()
    try:
        timeout = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "OPENCOLLAB_PUBLIC_PREPARATION_TIMEOUT_SECONDS must be finite and positive"
        ) from exc
    if not math.isfinite(timeout) or not 0 < timeout <= _MAX_PREPARATION_TIMEOUT_SECONDS:
        raise ValueError(
            "OPENCOLLAB_PUBLIC_PREPARATION_TIMEOUT_SECONDS must be finite and positive "
            "(at most 86400 seconds)"
        )
    return timeout


def run_public_preparation(script: Path, log_path: Path, workspace: Path) -> int:
    """Run one setup script and return its status after process-group cleanup."""
    _enable_subreaper()
    timeout = _preparation_timeout_seconds()
    if Path("/proc").is_dir():
        command = ["bash", str(script)]
    else:
        # Run in one shell so the EXIT trap can terminate asynchronous jobs
        # before a ``setsid`` child is reparented to launchd.  The monitor still
        # covers grandchildren that are not present in the shell job table.
        command = [
            "bash",
            "-c",
            (
                "trap 'status=$?; trap - EXIT; "
                'for pid in $(jobs -pr); do kill -KILL "$pid" 2>/dev/null; done; '
                'wait; exit "$status"' "' EXIT; . \"$1\""
            ),
            "opencollab-preparation-supervisor",
            str(script),
        ]
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        observed_descendants: set[int] = set()
        observed_identities: dict[int, str] = {}
        inspection_errors: list[BaseException] = []
        monitor_stop: threading.Event | None = None
        monitor: threading.Thread | None = None
        leader_identity: str | None = None
        process_group_id = process.pid
        leader_scan_root_missing = False
        if isinstance(process, _REAL_POPEN):
            try:
                process_group_id = os.getpgid(process.pid)
                state_identity = _process_state_identity(process.pid)
                if state_identity is not None and not state_identity[0].startswith("Z"):
                    leader_identity = state_identity[1]
                # ``None`` means the leader exited between ``getpgid`` and
                # the identity read.  That is a normal short-command race;
                # descendants are checked independently below.  Non-ENOENT
                # inspection errors are still recorded by the exception path.
            except (OSError, ProcessInspectionError) as exc:
                if process.poll() is None:
                    if isinstance(exc, ProcessInspectionError) and "omitted its root" in str(exc):
                        # The process can exit between ``poll`` and the first
                        # process-table query.  Defer this one race-sensitive
                        # error until the bounded wait tells us whether the
                        # leader really remained alive.
                        leader_scan_root_missing = True
                    else:
                        inspection_errors.append(exc)
        # Linux's child-subreaper path retains detached descendants after the
        # leader exits.  On macOS/BSD there is no subreaper, so sample the
        # tree while the leader is still alive and carry those PIDs into the
        # post-exit cleanup pass before the kernel reparents them to launchd.
        if isinstance(process, _REAL_POPEN) and not Path("/proc").is_dir():
            try:
                _merge_observed(
                    _descendants(process.pid),
                    observed_descendants,
                    observed_identities,
                    inspection_errors,
                )
            except (OSError, ProcessInspectionError) as exc:
                if process.poll() is None:
                    if isinstance(exc, ProcessInspectionError) and "omitted its root" in str(exc):
                        leader_scan_root_missing = True
                    else:
                        inspection_errors.append(exc)
            monitor_stop = threading.Event()
            monitor = threading.Thread(
                target=_observe_descendants,
                args=(
                    process,
                    monitor_stop,
                    observed_descendants,
                    inspection_errors,
                    observed_identities,
                ),
                name=f"opencollab-preparation-scan-{process.pid}",
                daemon=True,
            )
            monitor.start()
        timed_out = False
        pending_exception: BaseException | None = None
        status = 125
        try:
            status = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            status = 124
            log.write(
                (
                    "\npublic preparation timed out after "
                    f"{timeout:g} seconds; process cleanup started\n"
                ).encode()
            )
        except BaseException as exc:
            # KeyboardInterrupt/SystemExit must not bypass the process-group
            # cleanup below.  Preserve the original control-flow exception,
            # but defer re-raising it until the child and descendants have
            # received the same bounded teardown as ordinary failures.
            pending_exception = exc
        finally:
            if monitor_stop is not None:
                monitor_stop.set()
            if monitor is not None:
                monitor.join(timeout=_PROCESS_SCAN_TIMEOUT_SECONDS + 0.1)
        if leader_scan_root_missing and process.poll() is None:
            inspection_errors.append(
                ProcessInspectionError("preparation leader disappeared from process inspection")
            )
    try:
        if isinstance(process, _REAL_POPEN):
            leader_alive = process.poll() is None
            if (
                leader_identity is None
                and not leader_alive
                and _quiesce_group is _REAL_QUIESCE_GROUP
            ):
                # The original leader is gone and no start token was captured;
                # invoking killpg now could target a recycled PGID.  The
                # descendant scan below remains the authoritative cleanup.
                group_quiet = True
            else:
                try:
                    group_quiet = _quiesce_group(
                        process_group_id,
                        leader_identity,
                        leader_pid=process.pid,
                    )
                except TypeError as exc:
                    # A few embedders replace the historical one-argument hook
                    # with a test double.  Preserve that compatibility without
                    # swallowing unrelated implementation TypeErrors.
                    if "unexpected keyword argument" not in str(exc):
                        raise
                    group_quiet = _quiesce_group(process_group_id)
        else:
            # Keep lightweight test doubles and embedders compatible with the
            # historical one-argument cleanup hook.
            group_quiet = _quiesce_group(process_group_id)
        descendants_quiet = (
            _quiesce_descendants(observed_descendants, observed_identities)
            if observed_descendants
            else _quiesce_descendants()
        )
        if inspection_errors:
            # A failed ownership sample means a detached child could have
            # escaped between scans.  Do not claim a clean workspace unless
            # every required process-table query succeeded.
            descendants_quiet = False
        # ``_reap_children`` handles adopted descendants, but the Popen handle
        # itself must also be reaped with a finite wait to avoid a second hang.
        if process.poll() is None:
            process.wait(timeout=_CLEANUP_SECONDS)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return 125
    if pending_exception is not None:
        raise pending_exception
    if timed_out:
        return 124 if group_quiet and descendants_quiet else 125
    return status if group_quiet and descendants_quiet else 125


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        print("usage: public_preparation_runner.py SCRIPT LOG WORKSPACE", file=sys.stderr)
        return 2
    try:
        return run_public_preparation(Path(args[0]), Path(args[1]), Path(args[2]))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"public preparation failed: {exc}", file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
