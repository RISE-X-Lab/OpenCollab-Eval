"""Local batch-record transactions and persisted launch identity."""

from __future__ import annotations

import fcntl
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencollab_eval.engine.swe_eval_records import MAX_JSON_DOCUMENT_BYTES, MAX_JSONL_SCAN_BYTES
from opencollab_eval.experiment.batch_spec import SpecError, spec_digest
from opencollab_eval.safe_files import (
    open_regular_text_append,
    read_regular_bytes,
    read_regular_text,
    write_regular_bytes_atomic,
)

if TYPE_CHECKING:
    from opencollab_eval.commands.batch import Batch


MODEL_IDENTITY_FIELDS = ("model", "provider", "base_url_sha256")


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    # A stable inode matters here. Keep the empty lock file after release so a
    # second process never locks a new inode while the first still holds one.
    with open_regular_text_append(path) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def record_transaction(batch: Batch) -> Iterator[None]:
    """Serialize claims, identity checks, inputs, and batch.json writes."""
    with _file_lock(Path(batch.host.local_batches_dir) / ".batch-records.lock"):
        # Batch construction can precede another process's plan. Re-read both
        # parent and replacement claims after taking the same lock all writers
        # use, then keep it until the record has been committed.
        batch.check_retry()
        batch.check_replaces()
        yield


@contextmanager
def launch_transaction(batch: Batch) -> Iterator[None]:
    """Keep two launch/preflight callers for one name from sharing stale host facts."""
    with _file_lock(Path(batch.host.local_batches_dir) / f".{batch.spec.name}.launch.lock"):
        yield


def model_identity(facts: dict[str, Any]) -> dict[str, str]:
    """The existing non-secret host facts that identify one paid model run."""
    identity = {key: str(facts.get(key) or "") for key in MODEL_IDENTITY_FIELDS}
    # OpenCollab's provider config and client both default an unset provider
    # to openai. The host probe records the raw env line, which can be empty.
    identity["provider"] = (identity["provider"] or "openai").strip().lower() or "openai"
    return identity


def previous_record(batch: Batch, record: dict[str, Any] | None = None) -> dict[str, Any] | None:
    path = batch.record_path()
    if not path.exists():
        return None
    try:
        old = json.loads(read_regular_text(path, max_bytes=MAX_JSON_DOCUMENT_BYTES))
    except ValueError as exc:
        raise SpecError(f"batch {batch.spec.name!r}: existing {path} is not readable JSON ({exc})") from exc
    if not isinstance(old, dict):
        raise SpecError(f"batch {batch.spec.name!r}: existing {path} is not a batch record")
    changed = ["spec_digest"] if old.get("spec_digest") != spec_digest(batch.spec) else []
    if record is not None:
        old_spec = old.get("spec") or {}
        current_spec = record["spec"]
        if not isinstance(old_spec, dict) or {
            key: value for key, value in old_spec.items() if key != "concurrency"
        } != {key: value for key, value in current_spec.items() if key != "concurrency"}:
            changed.append("spec")
        for key in (
            "suite_sha256",
            "frame_content_sha256",
            "instances",
            "expected_card_sha256",
            "declared_role_profiles",
        ):
            if old.get(key) != record.get(key):
                changed.append(key)
        if not changed:
            inputs = batch.local_dir / record["instances"]["file"]
            if (
                inputs.exists()
                and hashlib.sha256(read_regular_bytes(inputs, max_bytes=MAX_JSONL_SCAN_BYTES)).hexdigest()
                != record["instances"]["sha256"]
            ):
                changed.append("instance file")
        if old.get("launches") and "host" in record:
            launches = old["launches"]
            first_identity = (launches[0] or {}).get("model_identity") or old.get("host") or {}
            if not isinstance(first_identity, dict):
                first_identity = {}
            paid = model_identity(first_identity)
            current = model_identity(record["host"])
            if not all(paid.values()) or paid != current:
                changed.append("paid model identity")
    if changed:
        raise SpecError(
            f"batch {batch.spec.name!r}: existing {path} has different {', '.join(changed)}; "
            "choose a new batch name to preserve its record and inputs"
        )
    return old


def save_record(batch: Batch, record: dict[str, Any]) -> Path:
    """Preserve host and launch history while writing one checked record."""
    old = batch.previous_record(record)
    if old is not None:
        if "host" not in record and "host" in old:
            record["host"] = old["host"]
        if "launches" not in record and "launches" in old:
            record["launches"] = old["launches"]
    path = batch.record_path()
    write_regular_bytes_atomic(path, (json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode())
    return path
