from __future__ import annotations

import pytest

from opencollab_eval.benchmarks.task_specification import (
    compose_task_specification,
)
from opencollab_eval.generation import gen_prediction_openhands as gpo


@pytest.mark.parametrize(
    ("instance", "expected"),
    [
        ({"problem_statement": "Fix it."}, "Fix it."),
        (
            {
                "problem_statement": "Fix it.",
                "requirements": "Keep compatibility.",
                "interface": "fix(value: str) -> str",
            },
            (
                "Fix it.\n\nRequirements:\nKeep compatibility.\n\n"
                "New interfaces introduced:\nfix(value: str) -> str"
            ),
        ),
        (
            {
                "problem_statement": "Fix it.\n\nRequirements:\nKeep compatibility.",
                "requirements": "Keep compatibility.",
            },
            "Fix it.\n\nRequirements:\nKeep compatibility.",
        ),
        (
            {
                "problem_statement": "Fix it.",
                "requirements": None,
                "interface": float("nan"),
            },
            "Fix it.",
        ),
    ],
)
def test_compose_task_specification(instance: dict, expected: str) -> None:
    assert compose_task_specification(instance) == expected


def test_compose_task_specification_ignores_sealed_fields() -> None:
    issue = compose_task_specification(
        {
            "problem_statement": "Fix it.",
            "requirements": "Keep compatibility.",
            "FAIL_TO_PASS": "private target",
            "test_patch": "private test patch",
            "patch": "private reference patch",
            "base_commit": "private commit",
            "instance_id": "private identity",
        }
    )

    assert issue == "Fix it.\n\nRequirements:\nKeep compatibility."


def test_openhands_prompt_and_solver_instance_receive_complete_task_specification() -> None:
    original_identity = "private-instance"
    public_identity = "solver-" + "a" * 32
    instance = {
        "instance_id": original_identity,
        "base_commit": "private-commit",
        "repo": "acme/widget",
        "problem_statement": "Fix the widget.",
        "requirements": "The widget must accept empty input.",
        "interface": "parse_widget(text: str) -> Widget",
        "hints_text": "Inspect parser.py.",
        "FAIL_TO_PASS": ["private target"],
        "test_patch": "private test patch",
    }

    prompt = gpo._prompt(instance, container_id="container-123")
    solver_instance = gpo._solver_instance(instance, public_identity)
    for task_text in (
        "Requirements:\nThe widget must accept empty input.",
        "New interfaces introduced:\nparse_widget(text: str) -> Widget",
    ):
        assert task_text in prompt
        assert task_text in solver_instance["problem_statement"]
    assert solver_instance["instance_id"] == public_identity
    assert solver_instance["hints_text"] == "Inspect parser.py."
    assert all(
        secret not in str(solver_instance)
        for secret in (original_identity, "private-commit", "private target", "private test patch")
    )
