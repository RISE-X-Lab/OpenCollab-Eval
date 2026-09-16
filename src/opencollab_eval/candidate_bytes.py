"""One bounded byte budget for candidate patches and their JSON containers.

Set OPENCOLLAB_EVAL_MAX_PATCH_BYTES before starting the controller. The default
supports 32 MiB of UTF-8 patch data, including patches containing JSON controls.
JSON can use six ASCII bytes for one input byte. Every serialized record and
pending envelope shares the existing 256 MiB ceiling, including all metadata.
The same ceiling bounds the largest configurable patch before JSON expansion.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

PATCH_BYTES_ENV = "OPENCOLLAB_EVAL_MAX_PATCH_BYTES"
MIB = 1024 * 1024
DEFAULT_PATCH_BYTES = 32 * MIB
MAX_CANDIDATE_FILE_BYTES = 256 * MIB
JSON_ESCAPE_FACTOR = 6
MAX_CONFIGURED_PATCH_BYTES = (MAX_CANDIDATE_FILE_BYTES // (JSON_ESCAPE_FACTOR * MIB)) * MIB


class CandidateByteLimitError(ValueError):
    """Candidate data exceeded a configured or derived finite byte budget."""


@dataclass(frozen=True)
class CandidateByteBudget:
    patch_bytes: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.patch_bytes, bool)
            or not isinstance(self.patch_bytes, int)
            or not 1 <= self.patch_bytes <= MAX_CONFIGURED_PATCH_BYTES
        ):
            raise ValueError(
                f"{PATCH_BYTES_ENV} must be an integer from 1 to {MAX_CONFIGURED_PATCH_BYTES}; "
                "the patch and worst-case JSON escaping must fit within "
                f"the {MAX_CANDIDATE_FILE_BYTES}-byte file budget"
            )

    @property
    def jsonl_line_bytes(self) -> int:
        return MAX_CANDIDATE_FILE_BYTES

    @property
    def pending_bytes(self) -> int:
        return MAX_CANDIDATE_FILE_BYTES


def budget_from_value(value: str | None) -> CandidateByteBudget:
    if value is None:
        return CandidateByteBudget(DEFAULT_PATCH_BYTES)
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+", value) is None:
        raise ValueError(f"{PATCH_BYTES_ENV} must contain an integer number of bytes")
    return CandidateByteBudget(int(value))


CANDIDATE_BYTE_BUDGET = budget_from_value(os.environ.get(PATCH_BYTES_ENV))


def candidate_byte_environment() -> dict[str, str]:
    """Carry the controller's effective budget across a fresh process boundary."""
    return {PATCH_BYTES_ENV: str(CANDIDATE_BYTE_BUDGET.patch_bytes)}


def validate_candidate_record(
    row: dict[str, Any],
    *,
    patch_key: str = "model_patch",
    budget: CandidateByteBudget = CANDIDATE_BYTE_BUDGET,
) -> None:
    if patch_key not in row:
        return
    patch = row[patch_key]
    if not isinstance(patch, str):
        # Existing record consumers handle absent/non-string patches as empty.
        return
    size = len(patch.encode("utf-8", errors="strict"))
    if size > budget.patch_bytes:
        raise CandidateByteLimitError(f"candidate patch is {size} bytes; limit is {budget.patch_bytes}")


def encode_candidate_jsonl(
    row: dict[str, Any],
    *,
    patch_key: str = "model_patch",
    budget: CandidateByteBudget = CANDIDATE_BYTE_BUDGET,
) -> bytes:
    validate_candidate_record(row, patch_key=patch_key, budget=budget)
    payload = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > budget.jsonl_line_bytes:
        raise CandidateByteLimitError(
            f"candidate JSONL row is {len(payload)} bytes; limit is {budget.jsonl_line_bytes}"
        )
    return payload


def encode_pending_candidate(
    candidate: dict[str, Any], *, budget: CandidateByteBudget = CANDIDATE_BYTE_BUDGET
) -> bytes:
    validate_candidate_record(candidate["prediction"], budget=budget)
    payload = (json.dumps(candidate, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > budget.pending_bytes:
        raise CandidateByteLimitError(f"pending output is {len(payload)} bytes; limit is {budget.pending_bytes}")
    return payload


def validate_candidate_read(row: dict[str, Any], error_type: type[Exception]) -> None:
    """Keep each reader's existing limit-error type at its public boundary."""
    try:
        validate_candidate_record(row)
    except CandidateByteLimitError as error:
        raise error_type(str(error)) from error
