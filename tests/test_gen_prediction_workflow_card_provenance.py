"""Which role cards a team run records, and why the record is a digest.

Split out of ``test_gen_prediction_workflow_generation`` to keep that file
inside the repository's per-file line limit.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from gen_prediction_workflow_support import (
    FIXTURE,
    gpw,
)
from gen_prediction_workflow_support import (
    isolated_solver_snapshot as _isolated_solver_snapshot,  # noqa: F401
)
from gen_prediction_workflow_support import (
    trusted_proof as _trusted_proof,
)

from opencollab_eval.engine.evaluator import EvalResult
from opencollab_eval.runtime_config import resolve_runtime_config


def _team_run(monkeypatch, tmp_path, *, team_config):
    """Run ``generate`` far enough to inspect the metrics row it builds."""

    async def fake_run_eval_task(task, **kwargs):
        return EvalResult(
            task_id=task.task_id,
            patch="diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n",
            patch_produced=True,
            tokens_used=1,
            steps=1,
            duration=1.0,
            workflow_result={"allowed_patch_paths": ["pkg/a.py"]},
        )

    monkeypatch.setattr(gpw, "run_eval_task", fake_run_eval_task)
    monkeypatch.setattr(
        gpw.gp, "start_container", lambda image, name, owner_token: "cid"
    )
    monkeypatch.setattr(
        gpw.gp, "remove_container_and_clear_marker", lambda run_dir, cid: True
    )
    monkeypatch.setattr(
        gpw,
        "extract_patch_guarded",
        lambda *args, **kwargs: (
            "diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n",
            [],
            _trusted_proof("diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n"),
        ),
    )
    args = SimpleNamespace(
        timeout=10,
        budget=1000,
        max_steps=3,
        keep_container=False,
        blind_validation=True,
        checkpoint_interval_seconds=0,
        resume=False,
        output=str(tmp_path / "predictions.jsonl"),
    )
    cfg = resolve_runtime_config(
        tmp_path,
        overrides={
            "model": "m",
            "provider": "openai",
            "api_key": "fixture-" + "A" * 24,
            "base_url": "https://model.example.invalid/v1",
            "temperature": 0.0,
            "thinking": False,
        },
    )
    _patch, metrics = asyncio.run(
        gpw.generate(
            FIXTURE,
            "image",
            cfg,
            args,
            gpw.generate_review_fix,
            "generate_review_fix",
            team_config=team_config,
        )
    )
    return metrics


def _write_team(tmp_path, analyst_card: str) -> str:
    (tmp_path / "analyst.md").write_text(analyst_card, encoding="utf-8")
    team_file = tmp_path / "team.yaml"
    team_file.write_text(
        "entry: analyst\nroles:\n  analyst:\n    prompt_file: analyst.md\n",
        encoding="utf-8",
    )
    return str(team_file)


def test_a_team_run_records_which_cards_it_was_seated_with(monkeypatch, tmp_path):
    """The grouping key of the handoff experiment's estimand.

    Its treatment is the wording of the analyst card, so a finished batch can
    only be split back into its conditions if every run recorded the card it
    ran. Recording the path would not do it: a first generation of these cards
    was moved under ``legacy/`` while the names stayed, so a path says where a
    card was, and only the digest says which card it was.
    """
    first = _team_run(
        monkeypatch, tmp_path, team_config=_write_team(tmp_path, "decide for yourself")
    )
    second = _team_run(
        monkeypatch, tmp_path, team_config=_write_team(tmp_path, "hand the work over")
    )

    assert first["role_prompt_sha256"]["analyst"] != second["role_prompt_sha256"]["analyst"]
    assert first["team_config_path"] == second["team_config_path"]  # same path, other card


def test_an_arm_with_no_cards_omits_the_key_rather_than_writing_null(
    monkeypatch, tmp_path
):
    """An arm that seats no cards and a recorder that broke must not read alike."""
    metrics = _team_run(monkeypatch, tmp_path, team_config=None)

    assert "role_prompt_sha256" not in metrics
    assert "team_config_path" not in metrics
