"""Prepare task-specific verifier inputs in an isolated Terminal scoring clone."""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import shlex
import tarfile
import tempfile
import uuid
from pathlib import Path

from . import terminal_container_backend as backend

FRAME_PATH = "/tmp/frame.bmp"
MIPS_TASKS = {"make-doom-for-mips", "make-mips-interpreter"}
_STAT_FORMAT = "%f\t%d\t%i\t%s\t%y\t%z"
_MOVED_STAT_FORMAT = "%f\t%d\t%i\t%s\t%y"
_INSPECT_FRAME = rf"""
if [ -L {FRAME_PATH} ]; then kind=symlink
elif [ -f {FRAME_PATH} ]; then kind=file
elif [ -d {FRAME_PATH} ]; then kind=directory
elif [ -e {FRAME_PATH} ]; then kind=other
else printf 'missing\n'; exit 0
fi
printf '%s\n' "$kind"
LC_ALL=C stat --printf='{_STAT_FORMAT}\n' -- {FRAME_PATH}
"""


class VerifierPreparationError(RuntimeError):
    """A scoring input could not be prepared; the verifier has not run."""

    def __init__(self, reason: str, message: str, *, receipt: dict | None = None):
        super().__init__(message)
        self.reason = reason
        self.receipt = receipt or {}


def _save_frame(artifacts: Path, archive_bytes: bytes, original: dict) -> tuple[dict, bytes]:
    """Read a single regular member without extracting paths or following links."""
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
            members = archive.getmembers()
            if len(members) != 1 or members[0].name not in {"frame.bmp", "./frame.bmp"} or not members[0].isfile():
                raise VerifierPreparationError(
                    "unsafe_frame_archive", "Frame archive must contain one regular frame.bmp"
                )
            member = members[0]
            stream = archive.extractfile(member)
            if stream is None:
                raise VerifierPreparationError("unsafe_frame_archive", "Frame archive content is unavailable")
            content = stream.read()
            if len(content) != member.size:
                raise VerifierPreparationError("invalid_frame_archive", "Frame archive content is truncated")
            if member.mode != original["mode"] or member.size != original["size"]:
                raise VerifierPreparationError("frame_changed", "Frame attributes changed while being archived")
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise VerifierPreparationError("invalid_frame_archive", "Frame archive could not be read") from exc

    paths = {}
    try:
        artifacts.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="terminal-verifier-frame-", dir=artifacts))
        paths = {"archive_path": str(directory / "frame.bmp.tar"), "content_path": str(directory / "frame.bmp")}
        for name, data in (("frame.bmp.tar", archive_bytes), ("frame.bmp", content)):
            destination = directory / name
            with destination.open("xb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            if destination.read_bytes() != data:
                raise OSError("frame backup readback differs")
        with (directory / "frame.json").open("x") as output:
            json.dump({"original_path": FRAME_PATH, **original}, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        raise VerifierPreparationError(
            "frame_backup_failed", "Frame backup could not be saved and read back", receipt=paths
        ) from exc
    return paths, content


def _move_frame_command(original: dict, quarantine: str) -> str:
    # Retain the moved object even after success. An open writer can therefore
    # never lose a changed file through an unlink after a content comparison.
    before = shlex.quote(original["stat"])
    after = shlex.quote(original["stat"].rsplit("\t", 1)[0])
    directory = shlex.quote(str(Path(quarantine).parent))
    held = shlex.quote(quarantine)
    return rf"""
directory={directory}
held={held}
created=0
restore_status=original_path
restore() {{
  if [ "$created" = 1 ]; then
    if [ -e "$held" ] || [ -L "$held" ]; then
      mv -nT -- "$held" {FRAME_PATH} 2>/dev/null || true
      if [ -e "$held" ] || [ -L "$held" ]; then restore_status=quarantine_path; fi
    fi
    if [ ! -e "$held" ] && [ ! -L "$held" ]; then rmdir -- "$directory" 2>/dev/null || true; fi
  fi
}}
fail() {{
  restore
  trap - EXIT
  printf '%s\n%s\n' "$1" "$restore_status"
  exit "$2"
}}
trap restore EXIT
mkdir -m 700 -- "$directory" || fail frame_move_failed 41
created=1
if [ -L {FRAME_PATH} ] || [ ! -f {FRAME_PATH} ]; then fail frame_changed 42; fi
current=$(LC_ALL=C stat --printf='{_STAT_FORMAT}' -- {FRAME_PATH}) || fail frame_inspection_failed 43
[ "$current" = {before} ] || fail frame_changed 44
mv -T -- {FRAME_PATH} "$held" || fail frame_move_failed 45
if [ -L "$held" ] || [ ! -f "$held" ]; then fail frame_changed 46; fi
current=$(LC_ALL=C stat --printf='{_MOVED_STAT_FORMAT}' -- "$held") || fail frame_inspection_failed 47
[ "$current" = {after} ] || fail frame_changed 48
cmp -s -- "$held" - || fail frame_changed 49
if [ -e {FRAME_PATH} ] || [ -L {FRAME_PATH} ]; then fail frame_changed 50; fi
trap - EXIT
printf 'moved\n'
"""


async def prepare_terminal_verifier(
    task_name: str,
    environment,
    artifacts: Path,
    *,
    login_probe: str | None = None,
    timeout: float = 30.0,
) -> dict:
    """Prepare an isolated verifier clone and return evidence of any changes.

    The trusted runner supplies the SSH login probe for configure-git-webserver.
    Its text and output are kept out of command logs and returned evidence.
    """
    receipt = {"task_name": task_name, "status": "ready", "action": "none"}
    if task_name not in MIPS_TASKS and task_name != "configure-git-webserver":
        return receipt
    if not math.isfinite(timeout) or timeout <= 0:
        raise VerifierPreparationError("invalid_preparation_timeout", "Preparation timeout must be positive and finite")
    deadline = asyncio.get_running_loop().time() + timeout

    def remaining():
        value = deadline - asyncio.get_running_loop().time()
        if value <= 0:
            raise asyncio.TimeoutError
        return value

    async def execute(command, *, stdin=None):
        return await environment.exec_cmd(command, timeout=remaining(), stdin=stdin)

    async def prepare():
        try:
            await environment.ensure_quiescent()
        except Exception as exc:
            raise VerifierPreparationError(
                "workspace_not_quiescent", "Verifier clone did not become quiescent"
            ) from exc
        if task_name == "configure-git-webserver":
            if login_probe is None or not login_probe.strip():
                raise VerifierPreparationError("login_probe_missing", "Trusted runner must supply an SSH login probe")
            receipt["action"] = "login_probe"
            try:
                result = await execute("/bin/bash -e -o pipefail -s >/dev/null 2>&1", stdin=login_probe.encode())
            except Exception as exc:
                raise VerifierPreparationError("login_probe_failed", "SSH login probe could not execute") from exc
            receipt["login_probe_exit_code"] = result.returncode
            if result.returncode:
                raise VerifierPreparationError("login_probe_failed", f"SSH login probe exited with {result.returncode}")
            return receipt

        receipt.update(action="remove_stale_frame", original_path=FRAME_PATH)
        try:
            result = await execute(_INSPECT_FRAME)
            if result.returncode:
                raise ValueError("frame inspection command failed")
            lines = result.stdout.splitlines()
            kind = lines[0]
            if kind == "missing":
                receipt["action"] = "frame_absent"
                return receipt
            values = lines[1].split("\t")
            if len(values) != 6:
                raise ValueError("frame inspection attributes are incomplete")
            original = {"type": kind, "mode": int(values[0], 16) & 0o7777, "size": int(values[3]), "stat": lines[1]}
            receipt["original"] = original
        except Exception as exc:
            raise VerifierPreparationError(
                "frame_inspection_failed", "Frame attributes could not be inspected"
            ) from exc
        if kind != "file":
            raise VerifierPreparationError("unsupported_frame_type", f"Frame is a {kind}; original is retained")
        try:
            archive_bytes = await asyncio.to_thread(
                backend.docker,
                "cp",
                environment.container + ":" + FRAME_PATH,
                "-",
                timeout=remaining(),
            )
        except Exception as exc:
            raise VerifierPreparationError("frame_archive_failed", "Frame archive could not be captured") from exc
        paths, content = await asyncio.to_thread(_save_frame, Path(artifacts), archive_bytes, original)
        receipt.update(paths)
        quarantine = "/tmp/.opencollab-verifier-frame-" + uuid.uuid4().hex + "/frame.bmp"
        receipt["quarantine_path"] = quarantine
        try:
            result = await execute(_move_frame_command(original, quarantine), stdin=content)
        except Exception as exc:
            raise VerifierPreparationError(
                "frame_move_failed", "Frame move did not complete; inspect original and quarantine"
            ) from exc
        if result.returncode or result.stdout.strip() != "moved":
            lines = result.stdout.splitlines()
            reason = (
                lines[0]
                if lines and lines[0] in {"frame_changed", "frame_move_failed", "frame_inspection_failed"}
                else "frame_move_failed"
            )
            if len(lines) > 1 and lines[1] in {"original_path", "quarantine_path"}:
                receipt["retained_at"] = receipt[lines[1]]
            raise VerifierPreparationError(reason, "Frame move failed; original or quarantine retains the file")
        return receipt

    try:
        return await asyncio.wait_for(prepare(), timeout)
    except VerifierPreparationError as exc:
        exc.receipt = {**receipt, **exc.receipt, "status": "error", "reason": exc.reason}
        raise
    except asyncio.CancelledError as exc:
        exc.receipt = {**receipt, "status": "error", "reason": "preparation_cancelled"}
        raise
    except asyncio.TimeoutError as exc:
        raise VerifierPreparationError(
            "preparation_timeout",
            "Verifier preparation exceeded its timeout",
            receipt={**receipt, "status": "error", "reason": "preparation_timeout"},
        ) from exc
