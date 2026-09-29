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
from opencollab_eval.experiment.batch_spec import SpecError, spec_digest, spec_identity
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
def record_transaction(batch: Batch, host_facts: dict[str, Any] | None = None) -> Iterator[None]:
    """Serialize claims, identity checks, inputs, and batch.json writes."""
    with _file_lock(Path(batch.host.local_batches_dir) / ".batch-records.lock"):
        # Batch construction can precede another process's plan. Re-read both
        # parent and replacement claims after taking the same lock all writers
        # use, then keep it until the record has been committed.
        batch.check_retry()
        batch.check_replaces()
        if host_facts is not None:
            check_cell_model_identity(batch, host_facts)
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


def cell_model_family(
    root: Path, name: str, current_spec: dict[str, Any] | None = None
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Find the cell connected by retries and replacements in one record scan."""
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*.launch/batch.json")):
        try:
            record = json.loads(read_regular_text(path, max_bytes=MAX_JSON_DOCUMENT_BYTES))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        records[path.parent.name.removesuffix(".launch")] = record

    specs = {member: record.get("spec") for member, record in records.items()}
    if current_spec is not None:
        specs[name] = current_spec

    def parent_of(member: str, spec: Any) -> str | None:
        if not isinstance(spec, dict):
            raise SpecError(f"batch {member!r}: its batch record has no readable spec")
        retry = spec.get("retry_of")
        replaces = spec.get("replaces")
        if retry is not None and replaces is not None:
            raise SpecError(f"batch {member!r}: retry_of and replaces cannot both be set")
        if replaces is not None:
            if (
                not isinstance(replaces, dict)
                or not isinstance(replaces.get("batch"), str)
                or not replaces["batch"]
            ):
                raise SpecError(f"batch {member!r}: invalid replaces relation")
            parent = replaces["batch"]
        elif retry is not None:
            if not isinstance(retry, str) or not retry:
                raise SpecError(f"batch {member!r}: invalid retry_of relation")
            parent = retry
        else:
            return None
        if parent == member:
            raise SpecError(f"batch {member!r}: relation points to itself")
        return parent

    children: dict[str, list[str]] = {}
    for member, spec in specs.items():
        if not isinstance(spec, dict):
            continue
        replacement = spec.get("replaces")
        for relation in (spec.get("retry_of"), replacement.get("batch") if isinstance(replacement, dict) else None):
            if isinstance(relation, str) and relation:
                children.setdefault(relation, []).append(member)

    family: list[str] = []
    seen: set[str] = set()
    ready = [name]
    while ready:
        member = ready.pop()
        if member in seen:
            continue
        seen.add(member)
        family.append(member)
        spec = specs.get(member)
        if spec is None:
            raise SpecError(f"batch {member!r}: its batch record is missing or unreadable")
        parent = parent_of(member, spec)
        if parent is not None:
            ready.append(parent)
        ready.extend(children.get(member, []))

    done: set[str] = set()
    for member in family:
        chain: set[str] = set()
        cursor: str | None = member
        while cursor is not None and cursor not in done:
            if cursor in chain:
                raise SpecError(f"batch relation cycle reaches {cursor!r}")
            chain.add(cursor)
            cursor = parent_of(cursor, specs[cursor])
        done.update(chain)
    return family, records


def check_cell_model_identity(batch: Batch, host_facts: dict[str, Any]) -> None:
    """Compare a launch with every paid attempt in its cell."""
    current = model_identity(host_facts)
    family, records = cell_model_family(
        Path(batch.host.local_batches_dir), batch.spec.name, spec_identity(batch.spec)
    )

    for name in family:
        record = records.get(name)
        if record is None:
            continue
        launches = record.get("launches") or []
        if not isinstance(launches, list):
            raise SpecError(f"cell member {name!r}: launches must be a list")
        for launch in launches:
            if not isinstance(launch, dict):
                raise SpecError(f"cell member {name!r}: launch must be an object")
            source = launch.get("model_identity") or record.get("host") or {}
            paid = model_identity(source if isinstance(source, dict) else {})
            if not all(paid.values()) or paid != current:
                raise SpecError(
                    f"batch {batch.spec.name!r}: paid model identity in cell member {name!r} "
                    f"{paid} differs from the effective model identity {current}"
                )


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
