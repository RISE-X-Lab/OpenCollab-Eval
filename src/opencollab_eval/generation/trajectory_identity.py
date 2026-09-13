"""Bound each JSONL record while retaining the existing Responses checks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from opencollab_eval.engine.swe_eval_records import open_regular_binary


def verified_provider_models(
    trajectory_path: str | None,
    *,
    artifact_root: Path,
    expected_model: str,
    expected_reasoning_effort: str | None,
    wire_protocol: str,
) -> tuple[list[str], str | None]:
    if wire_protocol != "responses":
        return [], None
    if not trajectory_path:
        raise RuntimeError("Responses execution did not produce a trajectory")
    path = Path(trajectory_path)
    models: set[str] = set()
    digest = hashlib.sha256()
    max_record_bytes = 16 * 1024 * 1024
    verification_error = None
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(artifact_root.resolve(strict=True)):
            raise RuntimeError("Responses trajectory is outside the current artifact root")
        with open_regular_binary(path) as handle:
            before = os.fstat(handle.fileno())
            # Read the opening snapshot only. A concurrent append is detected
            # by the original stat comparison instead of extending this loop.
            remaining = before.st_size
            try:
                while remaining:
                    raw_line = handle.readline(min(max_record_bytes + 1, remaining))
                    if not raw_line:
                        break
                    remaining -= len(raw_line)
                    if len(raw_line) > max_record_bytes:
                        raise RuntimeError("Responses trajectory record exceeds 16 MiB")
                    digest.update(raw_line)
                    for line in raw_line.decode("utf-8").splitlines():
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise RuntimeError("Responses trajectory contains invalid JSON") from exc
                        if not isinstance(record, dict) or record.get("type") != "llm_call":
                            continue
                        payload = record.get("payload")
                        if not isinstance(payload, dict):
                            raise RuntimeError("Responses llm_call is missing its payload")
                        if payload.get("wire_protocol") != "responses":
                            raise RuntimeError("Responses trajectory contains a mixed wire protocol")
                        observed = payload.get("provider_model")
                        if observed != expected_model:
                            raise RuntimeError(
                                f"Responses provider model mismatch expected {expected_model!r} got {observed!r}"
                            )
                        observed_effort = payload.get("reasoning_effort")
                        effort_policy = payload.get("reasoning_effort_policy")
                        if effort_policy not in {"configured", "suppressed"}:
                            raise RuntimeError(
                                "Responses llm_call is missing its reasoning effort policy"
                            )
                        expected_effort = (
                            None if effort_policy == "suppressed" else expected_reasoning_effort
                        )
                        if observed_effort != expected_effort:
                            raise RuntimeError(
                                "Responses reasoning effort mismatch "
                                f"expected {expected_effort!r} got {observed_effort!r}"
                            )
                        models.add(observed)
            except (RuntimeError, UnicodeDecodeError) as exc:
                verification_error = exc
            after = os.fstat(handle.fileno())
        if (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise RuntimeError("Responses trajectory changed while reading")
        if verification_error is not None:
            raise verification_error
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("Responses trajectory cannot be read") from exc
    if not models:
        raise RuntimeError("Responses trajectory contains no verified LLM call")
    return sorted(models), digest.hexdigest()
