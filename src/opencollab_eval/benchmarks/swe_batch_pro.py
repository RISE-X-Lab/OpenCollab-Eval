"""SWE-Batch Pro task normalization with a sealed solver boundary."""

from __future__ import annotations

import hashlib
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
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("dataset must be a regular file")
        if opened.st_size > MAX_DATASET_BYTES:
            raise ValueError("dataset exceeds the size limit")
        payload = bytearray()
        while len(payload) <= MAX_DATASET_BYTES:
            chunk = os.read(fd, min(1024 * 1024, MAX_DATASET_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > MAX_DATASET_BYTES:
            raise ValueError("dataset exceeds the size limit")
    finally:
        os.close(fd)

    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(bytes(payload).splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"dataset line {line_number} must be an object")
        rows.append(value)
    return rows


def task_from_row(row: Mapping[str, Any]) -> BenchmarkTask:
    instance_id = _first_string(row, "instance_id", "task_id", "id")
    repo = _first_string(row, "repo", "repository", "repo_name")
    problem = _first_string(row, "problem_statement", "problem", "description")
    if not instance_id or not repo or not problem:
        raise ValueError("task row requires instance_id, repo, and problem statement")
    identity = f"{repo}\0{problem}".encode()
    public_id = "solver-" + hashlib.sha256(identity).hexdigest()[:32]
    image = _first_string(row, "docker_image", "image")
    tag = _first_string(row, "dockerhub_tag", "image_tag")
    if not image and tag:
        image = PROLITE_IMAGE_PREFIX + tag
    public_metadata = row.get("solver_public_metadata")
    public_hints = row.get("solver_public_hints")
    return BenchmarkTask(
        public=PublicTask(
            task_id=public_id,
            repo=repo,
            problem_statement=problem,
            hints=_string_tuple(public_hints),
            metadata=dict(public_metadata) if isinstance(public_metadata, Mapping) else {},
        ),
        judge=JudgeSpec(
            instance_id=instance_id,
            base_commit=_first_string(row, "base_commit", "commit"),
            docker_image=image,
            fail_to_pass=_string_tuple(row.get("fail_to_pass") or row.get("FAIL_TO_PASS")),
            pass_to_pass=_string_tuple(row.get("pass_to_pass") or row.get("PASS_TO_PASS")),
            test_patch=str(row.get("test_patch") or ""),
        ),
    )


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


__all__ = ["load_jsonl_dataset", "task_from_row"]

