"""Usage and identity evidence shared by external solver adapters."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from opencollab_eval.engine.swe_generation_proof import MAX_TRUSTED_PATCH_BYTES
from opencollab_eval.usage import Usage, pricing_for_model, usage_cost_usd

from .candidate_patch_files import CandidateConstructionError
from .candidate_patch_git import canonicalize_candidate_patch

EXTERNAL_SIDECAR_SCHEMA = "opencollab.external_solver.v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _canonical_candidate_from_patch(
    git_dir: Path,
    anonymous_head: str,
    patch: str,
) -> tuple[str, str]:
    git = shutil.which("git")
    if not git:
        raise ValueError("trusted candidate Git executable is unavailable")
    try:
        return canonicalize_candidate_patch(
            git=git,
            git_dir=git_dir,
            base=anonymous_head,
            patch=patch,
        )
    except CandidateConstructionError as exc:
        raise ValueError(str(exc)) from exc


def _candidate_tree_from_patch(git_dir: Path, anonymous_head: str, patch: str) -> str:
    return _canonical_candidate_from_patch(git_dir, anonymous_head, patch)[0]


def _raw_candidate_patch(output_dir: Path, expected_sha256: str) -> str:
    path = output_dir / "claude.patch"
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError("external solver raw patch is missing") from exc
    if path.is_symlink() or not path.is_file() or info.st_size > MAX_TRUSTED_PATCH_BYTES:
        raise ValueError("external solver raw patch is unsafe")
    try:
        payload = path.read_bytes()
        patch = payload.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("external solver raw patch is unreadable") from exc
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError("external solver raw patch SHA mismatch")
    return patch


def _openhands_usage(output_dir: Path) -> dict[str, int | float] | None:
    state_paths = sorted(output_dir.rglob("base_state.json"), key=lambda path: path.stat().st_mtime)
    if not state_paths:
        return None
    try:
        state = json.loads(state_paths[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    stats = state.get("stats") if isinstance(state, dict) else None
    usage_map = stats.get("usage_to_metrics") if isinstance(stats, dict) else None
    if not isinstance(usage_map, dict):
        return None
    totals: dict[str, int | float] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "openhands_reported_cost_usd": 0.0,
    }
    for value in usage_map.values():
        if not isinstance(value, dict):
            continue
        accumulated = value.get("accumulated_token_usage")
        if isinstance(accumulated, dict):
            totals["input_tokens"] += int(accumulated.get("prompt_tokens") or 0)
            totals["output_tokens"] += int(accumulated.get("completion_tokens") or 0)
            totals["cache_read_tokens"] += int(accumulated.get("cache_read_tokens") or 0)
            totals["cache_creation_tokens"] += int(
                accumulated.get("cache_write_tokens") or accumulated.get("cache_creation_tokens") or 0
            )
        totals["openhands_reported_cost_usd"] += float(value.get("accumulated_cost") or 0.0)
    if not totals["input_tokens"] and not totals["output_tokens"]:
        return None
    totals["total_tokens"] = int(totals["input_tokens"]) + int(totals["output_tokens"])
    return totals


def _external_solver_evidence(output_dir: Path) -> dict[str, Any] | None:
    required_path = output_dir / "external_solver.required.json"
    sidecar_path = output_dir / "external_solver.sidecar.json"
    if not required_path.exists() and not sidecar_path.exists():
        return None
    try:
        required = json.loads(required_path.read_text(encoding="utf-8"))
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("external solver evidence is missing or malformed") from exc
    if not isinstance(required, dict) or not isinstance(sidecar, dict):
        raise ValueError("external solver evidence must contain JSON objects")
    solver = required.get("solver")
    expected_model = required.get("expected_model")
    if sidecar.get("schema") != EXTERNAL_SIDECAR_SCHEMA or sidecar.get("solver") != solver:
        raise ValueError("external solver sidecar identity mismatch")
    if sidecar.get("success") is not True:
        raise ValueError("external solver sidecar reports failure")
    if sidecar.get("expected_model") != expected_model:
        raise ValueError("external solver expected model mismatch")
    if sidecar.get("runtime_image_id") != required.get("expected_runtime_image_id"):
        raise ValueError("external solver runtime image mismatch")
    if sidecar.get("expected_runtime_image_id") != required.get("expected_runtime_image_id"):
        raise ValueError("external solver expected runtime image mismatch")
    if sidecar.get("task_image_id") != required.get("task_image_id"):
        raise ValueError("external solver task image mismatch")
    binding = sidecar.get("invocation_binding")
    if not isinstance(binding, dict) or binding.get("task_image_id") != required.get(
        "task_image_id"
    ):
        raise ValueError("external solver task image binding mismatch")
    if sidecar.get("cli_version") != sidecar.get("expected_cli_version"):
        raise ValueError("external solver CLI version mismatch")
    if sidecar.get("stream_cli_version") != sidecar.get("expected_cli_version"):
        raise ValueError("external solver stream CLI version mismatch")
    if sidecar.get("observed_models") != [expected_model]:
        raise ValueError("external solver stream model mismatch")
    if sidecar.get("model_usage_models") != [expected_model]:
        raise ValueError("external solver usage model mismatch")
    for field in ("stream_sha256", "settings_sha256"):
        value = sidecar.get(field)
        if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
            raise ValueError(f"external solver sidecar has invalid {field}")
    executable = sidecar.get("executable")
    if (
        not isinstance(executable, dict)
        or SHA256_RE.fullmatch(str(executable.get("sha256") or "")) is None
    ):
        raise ValueError("external solver executable identity is missing")
    return sidecar


def _external_solver_usage_evidence(output_dir: Path) -> dict[str, Any] | None:
    path = output_dir / "external_solver.sidecar.json"
    if not path.exists():
        return None
    try:
        sidecar = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("external solver usage evidence is malformed") from exc
    if (
        not isinstance(sidecar, dict)
        or sidecar.get("schema") != EXTERNAL_SIDECAR_SCHEMA
        or not isinstance(sidecar.get("solver"), str)
        or not isinstance(sidecar.get("usage"), dict)
    ):
        raise ValueError("external solver usage evidence is incomplete")
    return sidecar


def _bind_external_solver_evidence(
    evidence: dict[str, Any],
    *,
    output_dir: Path,
    prompt_file: Path,
    solver_task_id: str,
    public_instance_id: str,
    anonymous_head: str,
    base_tree: str,
    trusted_extraction: dict[str, Any],
    baseline_git_dir: Path,
    patch: str,
    run_identity: dict[str, str],
) -> dict[str, Any]:
    binding = evidence.get("invocation_binding")
    if not isinstance(binding, dict):
        raise ValueError("external solver invocation binding is missing")
    prompt_sha256 = hashlib.sha256(prompt_file.read_bytes()).hexdigest()
    patch_sha256 = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    expected = {
        "solver_task_id": solver_task_id,
        "prompt_sha256": prompt_sha256,
        "anonymous_head": anonymous_head,
        "base_tree": base_tree,
    }
    if any(binding.get(key) != value for key, value in expected.items()):
        raise ValueError("external solver invocation binding does not match the extracted candidate")
    if trusted_extraction.get("fixed_anonymous_base") != anonymous_head:
        raise ValueError("external solver anonymous baseline does not match trusted extraction")
    if trusted_extraction.get("base_tree") != base_tree:
        raise ValueError("external solver base tree does not match trusted extraction")
    if trusted_extraction.get("patch_sha256") != patch_sha256:
        raise ValueError("external solver patch SHA does not match trusted extraction")
    raw_patch_sha256 = binding.get("raw_patch_sha256")
    if not isinstance(raw_patch_sha256, str) or SHA256_RE.fullmatch(raw_patch_sha256) is None:
        raise ValueError("external solver raw patch SHA is invalid")
    raw_patch = _raw_candidate_patch(output_dir, raw_patch_sha256)
    raw_candidate_tree, canonical_raw_patch = _canonical_candidate_from_patch(
        baseline_git_dir,
        anonymous_head,
        raw_patch,
    )
    if binding.get("candidate_tree") != raw_candidate_tree:
        raise ValueError("external solver raw candidate tree mismatch")
    trusted_candidate_tree = _candidate_tree_from_patch(
        baseline_git_dir, anonymous_head, patch
    )
    pre_sanitization_tree = trusted_extraction.get(
        "pre_sanitization_candidate_tree",
        trusted_candidate_tree,
    )
    if pre_sanitization_tree != raw_candidate_tree:
        raise ValueError("external solver candidate does not match trusted extraction")
    if (
        trusted_extraction.get("candidate_tree", trusted_candidate_tree)
        != trusted_candidate_tree
    ):
        raise ValueError("trusted final candidate tree mismatch")
    canonical_raw_sha256 = hashlib.sha256(canonical_raw_patch.encode()).hexdigest()
    if trusted_extraction.get(
        "pre_sanitization_patch_sha256",
        trusted_extraction.get("patch_sha256"),
    ) != canonical_raw_sha256:
        raise ValueError("external solver canonical patch does not match trusted extraction")
    task_image_id = binding.get("task_image_id")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", str(task_image_id or "")) is None:
        raise ValueError("external solver task image identity is invalid")
    evidence["evaluation_binding"] = {
        "public_instance_id": public_instance_id,
        "solver_task_id": solver_task_id,
        "trusted_baseline_sha256": trusted_extraction.get("baseline_archive_sha256"),
        "trusted_workspace_sha256": trusted_extraction.get("workspace_archive_sha256"),
        "raw_patch_sha256": raw_patch_sha256,
        "canonical_raw_patch_sha256": canonical_raw_sha256,
        "trusted_final_patch_sha256": patch_sha256,
        "raw_candidate_tree": raw_candidate_tree,
        "candidate_tree": trusted_candidate_tree,
        "task_image_id": task_image_id,
        **run_identity,
    }
    (output_dir / "external_solver.sidecar.json").write_text(
        json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8"
    )
    return evidence


def _external_solver_usage(evidence: dict[str, Any] | None) -> dict[str, int | float] | None:
    if not evidence:
        return None
    usage = evidence.get("usage")
    if not isinstance(usage, dict):
        return None
    fields = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
    values: dict[str, int | float] = {}
    for field in fields:
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        values[field] = value
    values["claude_reported_cost_usd"] = float(evidence.get("cost_usd") or 0.0)
    values["total_tokens"] = int(values["input_tokens"]) + int(values["output_tokens"])
    return values


def _append_usage_record(
    *,
    run_dir: Path,
    instance_id: str,
    model: str,
    usage_values: dict[str, int | float],
    provider: str = "openhands",
    label: str = "openhands-aggregate",
    status: str = "success",
) -> dict[str, Any]:
    usage = Usage(
        input_tokens=int(usage_values["input_tokens"]),
        output_tokens=int(usage_values["output_tokens"]),
        cache_read_tokens=int(usage_values["cache_read_tokens"]),
        cache_creation_tokens=int(usage_values["cache_creation_tokens"]),
    )
    estimated_cost = usage_cost_usd(usage, model)
    payload: dict[str, Any] = {
        "input_tokens": usage.input_tokens,
        "uncached_input_tokens": max(
            usage.input_tokens - usage.cache_read_tokens - usage.cache_creation_tokens, 0
        ),
        "cached_input_tokens": usage.cache_read_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "estimated": False,
        "cost_usd": estimated_cost,
        "pricing": pricing_for_model(model),
    }
    for field in ("openhands_reported_cost_usd", "claude_reported_cost_usd"):
        if field in usage_values:
            payload[field] = usage_values[field]
    if provider == "claude-code" and "claude_reported_cost_usd" in usage_values:
        payload["estimated_cost_usd"] = estimated_cost
        payload["cost_usd"] = float(usage_values["claude_reported_cost_usd"])
        payload["pricing"] = {"source": "provider_reported", "currency": "USD"}
    record = {
        "schema": "opencollab.api_usage.v1",
        "timestamp": time.time(),
        "request_id": str(uuid.uuid4()),
        "status": status,
        "provider": provider,
        "model": model,
        "latency_s": 0.0,
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "argv0": Path(sys.argv[0]).name if sys.argv else None,
        "run_id": instance_id,
        "label": label,
        "base_url": None,
        "base_url_host": None,
        "usage": payload,
    }
    with (run_dir / "api_usage.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return payload


__all__ = [
    "_append_usage_record",
    "_bind_external_solver_evidence",
    "_candidate_tree_from_patch",
    "_external_solver_evidence",
    "_external_solver_usage_evidence",
    "_external_solver_usage",
    "_openhands_usage",
]
