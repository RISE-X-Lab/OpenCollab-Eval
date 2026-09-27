"""Public evaluator entrypoints remain importable from an installed core package."""

from __future__ import annotations

import inspect

from opencollab_eval import cli
from opencollab_eval.engine.evaluator import EvalResult, EvalTask, run_eval_batch, run_eval_task
from opencollab_eval.generation import gen_prediction, gen_prediction_openhands, gen_prediction_workflow
from opencollab_eval.workflow_loader import load_workflow


def test_public_evaluator_bindings_remain_available() -> None:
    assert EvalTask(task_id="public-task", description="public issue").task_id == "public-task"
    assert (
        EvalResult(task_id="public-task", patch="", patch_produced=False, tokens_used=0, steps=0, duration=0).task_id
        == "public-task"
    )
    assert inspect.iscoroutinefunction(run_eval_task)
    assert inspect.iscoroutinefunction(run_eval_batch)
    assert callable(load_workflow)


def test_generic_cli_and_generation_modules_remain_available() -> None:
    assert callable(cli.main)
    for module in (gen_prediction, gen_prediction_workflow, gen_prediction_openhands):
        assert callable(module.main)
