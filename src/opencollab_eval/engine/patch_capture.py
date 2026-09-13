"""Read a complete diff independently of per-command display limits."""

from __future__ import annotations

import base64
import binascii
import posixpath
import shlex
from collections.abc import Awaitable, Callable
from typing import Any

from opencollab_eval.engine.async_runtime import add_exception_note

CHUNK_BYTES = 64 * 1024


def _checked_stdout(result: Any) -> str:
    if result.returncode != 0 or result.stdout_truncated or result.stderr_truncated:
        raise RuntimeError("complete diff transfer failed or was truncated")
    return result.stdout


async def capture_complete_diff(
    env: Any,
    command: Callable[[tuple[str, ...]], str],
    *,
    max_bytes: int,
    await_teardown: Callable[[Awaitable[Any]], Awaitable[Any]],
) -> tuple[str, Exception | None]:
    """Capture into an environment-owned file and transfer bounded byte chunks.

    Called only after writers have quiesced. The existing result-record limit
    still bounds the complete artifact. A disposal failure is returned alongside
    the captured patch, so callers can preserve it while withholding submission.
    """
    path = await await_teardown(env.write_temp_file("", prefix="opencollab-eval-diff-", suffix=".patch"))
    quoted = shlex.quote(path)
    relative = posixpath.relpath(path, env.workspace)
    exclusions = () if relative == ".." or relative.startswith("../") else (relative,)
    patch = ""
    failure: BaseException | None = None
    cleanup_error: Exception | None = None
    try:
        _checked_stdout(await await_teardown(env.exec_cmd(f"( {command(exclusions)}\n) > {quoted}")))
        size = int(_checked_stdout(await await_teardown(env.exec_cmd(f"wc -c < {quoted}"))).strip())
        if not 0 <= size <= max_bytes:
            raise ValueError(f"captured diff exceeds result record limit of {max_bytes} bytes")
        chunks = []
        for offset in range(0, size, CHUNK_BYTES):
            result = await await_teardown(env.exec_cmd(
                f"set -o pipefail; dd if={quoted} bs={CHUNK_BYTES} "
                f"skip={offset // CHUNK_BYTES} count=1 2>/dev/null | base64"
            ))
            encoded = "".join(_checked_stdout(result).split())
            try:
                chunk = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise RuntimeError("invalid complete diff transfer encoding") from exc
            if len(chunk) != min(CHUNK_BYTES, size - offset):
                raise RuntimeError("complete diff transfer returned an incomplete chunk")
            chunks.append(chunk)
        final_size = int(_checked_stdout(await await_teardown(env.exec_cmd(f"wc -c < {quoted}"))).strip())
        if final_size != size:
            raise RuntimeError("captured diff changed during transfer")
        patch = b"".join(chunks).decode("utf-8")
    except BaseException as exc:
        failure = exc
    finally:
        try:
            await await_teardown(env.remove_file(path))
        except Exception as exc:
            cleanup_error = exc
    if failure is not None:
        if cleanup_error is not None:
            add_exception_note(failure, f"captured diff temporary-file cleanup also failed: {cleanup_error}")
        raise failure
    return patch, cleanup_error
