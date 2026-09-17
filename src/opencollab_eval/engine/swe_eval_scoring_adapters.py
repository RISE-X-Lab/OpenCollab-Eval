"""Load externally configured adapters for isolated official-scoring rows."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import itertools
import json
import os
import sys
from pathlib import Path
from typing import Any

REGISTRY_ENV = "OPENCOLLAB_EVAL_SCORING_ADAPTER_REGISTRY"
REGISTRY_SCHEMA = "opencollab.scoring_adapter_registry.v1"
RECEIPT_SCHEMA = "opencollab.scoring_adapter_receipt.v1"
_MODULE_SEQUENCE = itertools.count()


class PreparedScoringRow(dict[str, Any]):
    """An in-memory scoring copy whose adapter has already executed."""

    def __init__(self, row: dict[str, Any], receipt: dict[str, Any]) -> None:
        super().__init__(row)
        self.scoring_adapter_receipt = receipt


def configured_registry_path(value: str | Path | None = None) -> Path | None:
    """Resolve a worker registry path, including the host configuration alias."""
    configured = value if value not in (None, "") else os.environ.get(REGISTRY_ENV)
    if configured not in (None, ""):
        if not isinstance(configured, (str, Path)) or not str(configured).strip():
            raise ValueError("scoring adapter registry must be an absolute worker path")
        path = Path(configured)
        if not path.is_absolute():
            raise ValueError("scoring adapter registry must be an absolute worker path")
        return path
    engine = Path(__file__).resolve().parent
    for path in (engine / "registry.json", engine / "scoring_adapters/registry.json"):
        if path.is_file():
            return path
    return None


def adapt_instance(
    instance: dict[str, Any], registry_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply one registered adapter and retain its existing isolation checks."""
    if not isinstance(instance, dict):
        raise TypeError("instance must be a dictionary")
    instance_id = instance.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise ValueError("instance_id is required")
    path = configured_registry_path(registry_path)
    receipt = {
        "schema": RECEIPT_SCHEMA, "applied": False,
        "instance_id": instance_id, "registry": str(path) if path else None,
    }
    if path is None:
        return copy.deepcopy(instance), receipt
    registry = json.loads(path.read_text())
    if not isinstance(registry, dict) or registry.get("schema") != REGISTRY_SCHEMA:
        raise ValueError(f"unsupported scoring adapter registry in {path}")
    entries = registry.get("entries")
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError(f"scoring adapter registry entries must be objects in {path}")
    matches = [entry for entry in entries if entry.get("instance_id") == instance_id]
    if len(matches) > 1:
        raise ValueError(f"multiple scoring adapters registered for {instance_id}")
    if not matches:
        return copy.deepcopy(instance), receipt
    entry = matches[0]
    module_path = path.parent / str(entry.get("module") or "")
    spec = importlib.util.spec_from_file_location(f"opencollab_scoring_adapter_{next(_MODULE_SEQUENCE)}", module_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load scoring adapter module {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if sys.modules.get(spec.name) is module:
            del sys.modules[spec.name]
        raise
    if instance_id not in getattr(module, "INSTANCE_IDS", ()):
        raise ValueError("registry instance is absent from adapter INSTANCE_IDS")
    if getattr(module, "ADAPTER_ID", None) != entry.get("adapter_id"):
        raise ValueError(f"registry adapter_id differs from module ADAPTER_ID in {module_path}")
    original = copy.deepcopy(instance)
    adapted, adapter_receipt = module.adapt(copy.deepcopy(instance))
    if not isinstance(adapted, dict) or not isinstance(adapter_receipt, dict):
        raise ValueError("adapter must return two dictionaries")
    changed = sorted(
        key for key in set(original) | set(adapted)
        if original.get(key) != adapted.get(key) or (key in original) != (key in adapted)
    )
    allowed = sorted(entry.get("allowed_changed_fields") or [])
    if changed != allowed:
        raise ValueError(f"adapter changed {changed}, expected exactly {allowed}")
    if adapted.get("instance_id") != instance_id:
        raise ValueError("adapter changed instance_id")
    if adapter_receipt.get("adapter_id") != entry.get("adapter_id"):
        raise ValueError("adapter receipt has the wrong adapter_id")
    if sorted(adapter_receipt.get("changed_fields") or []) != allowed:
        raise ValueError("adapter receipt changed_fields differs from registry")
    if adapter_receipt.get("solver_input_unchanged") is not True:
        raise ValueError("adapter did not attest solver input isolation")
    if adapter_receipt.get("gold_production_code_added") is not False:
        raise ValueError("adapter did not attest production-code isolation")
    receipt = dict(adapter_receipt)
    receipt.update(applied=True, instance_id=instance_id, registry=str(path),
                   registry_schema=REGISTRY_SCHEMA, module=str(module_path))
    receipt.update(
        original_instance_sha256=hashlib.sha256(
            json.dumps(original, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        adapted_instance_sha256=hashlib.sha256(
            json.dumps(adapted, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    )
    return adapted, receipt


def prepare_scoring_row(
    row: dict[str, Any], registry_path: str | Path | None = None,
) -> PreparedScoringRow:
    """Prepare a scoring copy once across main, retries, and direct entrypoints."""
    if isinstance(row, PreparedScoringRow):
        return row
    adapted, receipt = adapt_instance(row, registry_path)
    return PreparedScoringRow(adapted, receipt)
