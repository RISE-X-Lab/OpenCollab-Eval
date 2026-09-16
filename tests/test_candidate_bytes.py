from __future__ import annotations

import pytest

from opencollab_eval.candidate_bytes import (
    CANDIDATE_BYTE_BUDGET,
    DEFAULT_PATCH_BYTES,
    MAX_CANDIDATE_FILE_BYTES,
    CandidateByteBudget,
    CandidateByteLimitError,
    budget_from_value,
    candidate_byte_environment,
    encode_candidate_jsonl,
    encode_pending_candidate,
)
from opencollab_eval.generation import gen_prediction_constants as constants


def test_default_budget_accepts_candidate_above_legacy_eight_mib_limit() -> None:
    patch = "x" * (8 * 1024 * 1024 + 1)

    payload = encode_candidate_jsonl({"instance_id": "task", "model_patch": patch})

    assert len(payload) > 8 * 1024 * 1024
    assert CANDIDATE_BYTE_BUDGET.patch_bytes == DEFAULT_PATCH_BYTES == 32 * 1024 * 1024
    assert constants.MAX_EXTRACTED_PATCH_BYTES == DEFAULT_PATCH_BYTES
    assert constants.MAX_OUTPUT_JSONL_BYTES == MAX_CANDIDATE_FILE_BYTES


def test_candidate_budget_bounds_patch_before_json_serialization() -> None:
    budget = CandidateByteBudget(4)

    with pytest.raises(CandidateByteLimitError, match="5 bytes; limit is 4"):
        encode_candidate_jsonl({"model_patch": "12345"}, budget=budget)
    with pytest.raises(CandidateByteLimitError, match="5 bytes; limit is 4"):
        encode_pending_candidate(
            {"prediction": {"model_patch": "12345"}, "metric": {}},
            budget=budget,
        )


@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", "letters"])
def test_candidate_budget_rejects_invalid_environment_values(value: str) -> None:
    with pytest.raises(ValueError, match="OPENCOLLAB_EVAL_MAX_PATCH_BYTES"):
        budget_from_value(value)


def test_candidate_budget_is_forwarded_across_runtime_processes() -> None:
    assert candidate_byte_environment() == {
        "OPENCOLLAB_EVAL_MAX_PATCH_BYTES": str(DEFAULT_PATCH_BYTES)
    }
