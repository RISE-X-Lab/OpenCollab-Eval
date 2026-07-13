from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from opencollab.sdk.workflows import load_workflow_specs

from opencollab_eval.engine.solver_backend import workflow_solver_spec

WORKFLOW_ROOT = Path(str(files("opencollab_eval.workflows")))


def test_default_solver_specs_match_live_runner_modes() -> None:
    assert workflow_solver_spec("g1.1").workflow_name == "validation-council-solve"
    assert workflow_solver_spec("g1.1").max_attempts == 3
    assert workflow_solver_spec("baseTeam").workflow_name == "base-team"
    team_pro = workflow_solver_spec("TeamPro")
    assert team_pro.workflow_name == "team-pro"
    assert team_pro.max_attempts == 3
    assert team_pro.default_budget_tokens == 4_000_000
    assert team_pro.required_runtime_options == (
        ("--model-name", "OPENCOLLAB_SWE_MODEL_NAME"),
        ("--llm-model", "OPENCOLLAB_SWE_LLM_MODEL"),
    )
    assert team_pro.config_overrides == {
        "temperature": 1.0,
        "top_p": 1.0,
        "max_output_tokens": 32_768,
    }
    assert workflow_solver_spec("openhands").workflow_name == "openhands-external"


def test_base_team_workflow_registers_without_reexporting_other_workflows() -> None:
    specs = load_workflow_specs(str(WORKFLOW_ROOT / "base_team.py"))
    names = {spec.name for spec in specs}

    assert names == {"base-team"}
    spec = specs[0]
    assert spec.phases == ("analyze", "code", "verify")


def test_team_pro_workflow_alias_preserves_dynamic_workflow_phases() -> None:
    specs = load_workflow_specs(str(WORKFLOW_ROOT / "analyst_solve.py"))
    by_name = {spec.name: spec for spec in specs}

    assert {"analyst-solve", "team-pro"} <= set(by_name)
    assert by_name["team-pro"].phases == ("scope", "recon", "plan", "implement", "verify")
