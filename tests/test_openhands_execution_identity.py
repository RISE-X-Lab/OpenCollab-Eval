from __future__ import annotations

from pathlib import Path

import pytest
from gen_prediction_openhands_support import write_openhands_state

from opencollab_eval.generation.external_solver_usage import (
    _openhands_execution_evidence,
)


def test_openhands_persisted_state_proves_real_model_calls(tmp_path: Path) -> None:
    write_openhands_state(tmp_path, "provider/model")

    evidence = _openhands_execution_evidence(tmp_path, "provider/model")

    assert evidence["schema"] == "opencollab.openhands_execution_identity.v1"
    assert evidence["model"] == "provider/model"
    assert evidence["state_file_count"] == 1
    assert evidence["llm_call_count"] == 1
    assert len(evidence["state_sha256"]) == 64


def test_openhands_persisted_state_rejects_missing_or_drifted_calls(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="state count"):
        _openhands_execution_evidence(tmp_path, "provider/model")

    write_openhands_state(tmp_path, "other-model")
    with pytest.raises(ValueError, match="does not match"):
        _openhands_execution_evidence(tmp_path, "provider/model")
