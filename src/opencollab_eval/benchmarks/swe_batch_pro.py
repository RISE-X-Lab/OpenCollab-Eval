"""SWE-Batch Pro task normalization with a sealed solver boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from opencollab_eval.contracts import BenchmarkTask, JudgeSpec, PublicTask

MAX_DATASET_BYTES = 64 * 1024 * 1024
PROLITE_IMAGE_PREFIX = "docker.1panel.live/jefzda/sweap-images:"


def load_jsonl_dataset(path: Path) -> list[dict[str, Any]]:
    payload = _read_regular_bytes(path, max_bytes=MAX_DATASET_BYTES, label="dataset")

    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(payload.splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"dataset line {line_number} must be an object")
        rows.append(value)
    return rows


def load_identity_key(path: Path) -> bytes:
    """Load an evaluator-owned raw 32-byte HMAC key without following symlinks."""

    key = _read_regular_bytes(path, max_bytes=32, label="identity key")
    if len(key) != 32:
        raise ValueError("identity key must contain exactly 32 raw bytes")
    return key


def _read_regular_bytes(path: Path, *, max_bytes: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"{label} must be a regular file")
        if opened.st_size > max_bytes:
            raise ValueError(f"{label} exceeds the size limit")
        payload = bytearray()
        while len(payload) <= max_bytes:
            chunk = os.read(fd, min(1024 * 1024, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > max_bytes:
            raise ValueError(f"{label} exceeds the size limit")
    finally:
        os.close(fd)
    return bytes(payload)


def task_from_row(row: Mapping[str, Any], *, identity_key: bytes) -> BenchmarkTask:
    if not isinstance(identity_key, bytes) or len(identity_key) < 32:
        raise ValueError("identity_key must contain at least 32 bytes")
    instance_id = _first_string(row, "instance_id", "task_id", "id")
    repo = _first_string(row, "repo", "repository", "repo_name")
    problem = _first_string(row, "problem_statement", "problem", "description")
    if not instance_id or not repo or not problem:
        raise ValueError("task row requires instance_id, repo, and problem statement")
    public_id = "solver-" + hmac.new(identity_key, instance_id.encode(), hashlib.sha256).hexdigest()[:32]
    image = _first_string(row, "docker_image", "image")
    tag = _first_string(row, "dockerhub_tag", "image_tag")
    if not image and tag:
        image = PROLITE_IMAGE_PREFIX + tag
    public_metadata = row.get("solver_public_metadata")
    public_hints = row.get("solver_public_hints")
    public = PublicTask(
        task_id=public_id,
        repo=repo,
        problem_statement=problem,
        hints=_string_tuple(public_hints),
        metadata=dict(public_metadata) if isinstance(public_metadata, Mapping) else {},
    )
    judge = JudgeSpec(
        instance_id=instance_id,
        base_commit=_first_string(row, "base_commit", "commit"),
        docker_image=image,
        fail_to_pass=_string_tuple(row.get("fail_to_pass") or row.get("FAIL_TO_PASS")),
        pass_to_pass=_string_tuple(row.get("pass_to_pass") or row.get("PASS_TO_PASS")),
        test_patch=str(row.get("test_patch") or ""),
    )
    _reject_sealed_values(public, judge)
    return BenchmarkTask(public=public, judge=judge)


def tasks_from_rows(rows: Iterable[Mapping[str, Any]], *, identity_key: bytes) -> list[BenchmarkTask]:
    """Normalize a batch and reject any ambiguous public identifier."""

    tasks: list[BenchmarkTask] = []
    identities: set[str] = set()
    for row in rows:
        task = task_from_row(row, identity_key=identity_key)
        if task.public.task_id in identities:
            raise ValueError("task batch contains a duplicate public identity")
        identities.add(task.public.task_id)
        tasks.append(task)
    return tasks


def _reject_sealed_values(public: PublicTask, judge: JudgeSpec) -> None:
    sealed = tuple(
        value.casefold()
        for value in _iter_strings(
            (
                judge.instance_id,
                judge.base_commit,
                judge.docker_image,
                judge.fail_to_pass,
                judge.pass_to_pass,
                judge.test_patch,
            )
        )
        if value.strip()
    )
    for candidate in _iter_strings((public.hints, public.metadata)):
        normalized = candidate.strip().casefold()
        if not normalized:
            continue
        for secret in sealed:
            if (
                normalized == secret
                or (len(secret) >= 8 and secret in normalized)
                or (len(normalized) >= 16 and normalized in secret)
            ):
                raise ValueError("public solver data contains sealed task information")


def _iter_strings(value: Any) -> tuple[str, ...]:
    strings: list[str] = []
    if isinstance(value, str):
        strings.append(value)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            strings.extend(_iter_strings(str(key)))
            strings.extend(_iter_strings(item))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            strings.extend(_iter_strings(item))
    return tuple(strings)


def _first_string(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ()
        if stripped.startswith("["):
            decoded = json.loads(stripped)
            if isinstance(decoded, list):
                return tuple(str(item) for item in decoded)
        return tuple(part.strip() for part in stripped.split(",") if part.strip())
    if isinstance(value, Iterable):
        return tuple(str(item) for item in value)
    return (str(value),)


__all__ = ["load_identity_key", "load_jsonl_dataset", "task_from_row", "tasks_from_rows"]
