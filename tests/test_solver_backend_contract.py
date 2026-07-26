from __future__ import annotations

from opencollab_eval.engine.solver_backend import workflow_solver_spec
from opencollab_eval.workflows.analyst_solve import analyst_solve, team_pro
from opencollab_eval.workflows.base_team import base_team


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
    claude = workflow_solver_spec("claude-code")
    assert claude.workflow_name == "openhands-external"
    assert claude.max_attempts == 1
    assert claude.args["max_empty_patch_retries"] == 0
    assert claude.args["max_eval_attempts"] == 1
    assert "run_claude_code_cli.sh" in claude.args["openhands_command"]


def test_base_team_workflow_registers_without_reexporting_other_workflows() -> None:
    spec = base_team.__workflow_spec__
    assert spec.name == "base-team"
    assert spec.phases == ("analyze", "code", "verify")


def test_team_pro_workflow_alias_preserves_dynamic_workflow_phases() -> None:
    assert analyst_solve.__workflow_spec__.name == "analyst-solve"
    assert team_pro.__workflow_spec__.name == "team-pro"
    assert team_pro.__workflow_spec__.phases == (
        "scope",
        "recon",
        "plan",
        "implement",
        "verify",
    )
