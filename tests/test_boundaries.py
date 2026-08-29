from __future__ import annotations

import ast
import importlib
from pathlib import Path

import opencollab_eval

_TEST_ROOT = Path(__file__).resolve().parent
_SOURCE = Path(opencollab_eval.__file__).resolve().parent


_PUBLIC_MODULES = frozenset(
    {
        "opencollab",
        "opencollab.environments",
        "opencollab.teams",
        "opencollab.tools",
        "opencollab.workflows",
    }
)
_PUBLIC_NAMES = {
    "opencollab": frozenset({"OpenCollab", "RunError", "RunResult", "workflow"}),
    "opencollab.environments": frozenset(
        {
            "Environment",
            "attach_container",
            # A listing of the workspace the agents actually read. It has to be
            # asked of the environment: the directory this process could walk
            # is the one the run was launched from, not the one in the task
            # container.
            "build_repo_map_via_env",
            "docker_environment",
            "local_environment",
            "worktree_environment",
        }
    ),
    "opencollab.teams": frozenset(
        {
            # How many seats a team file declares. The pool a team run is
            # started with is that count times one agent's budget, because the
            # scheduler caps each seat at pool/N -- so a caller that cannot
            # read the count cannot state the budget correctly.
            "declared_role_names",
        }
    ),
    "opencollab.tools": frozenset(
        {"BuiltinToolName", "Tool", "VerificationTool", "builtin_tools"}
    ),
    "opencollab.workflows": frozenset({"WorkflowContext", "workflow"}),
}


def _forbidden_opencollab_imports(tree: ast.AST) -> list[str]:
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name
                if (
                    module == "opencollab" or module.startswith("opencollab.")
                ) and module not in _PUBLIC_MODULES:
                    offenders.append(module)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
            if module == "opencollab":
                offenders.extend(
                    f"{module}.{alias.name}"
                    for alias in node.names
                    if alias.name not in _PUBLIC_NAMES[module]
                )
            elif (
                module == "opencollab" or module.startswith("opencollab.")
            ) and module not in _PUBLIC_MODULES:
                offenders.append(module)
            elif module in _PUBLIC_MODULES:
                offenders.extend(
                    f"{module}.{alias.name}"
                    for alias in node.names
                    if alias.name not in _PUBLIC_NAMES[module]
                )
    return offenders


def _retired_sdk_imports(tree: ast.AST) -> list[str]:
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name
                if module == "opencollab.sdk" or module.startswith("opencollab.sdk."):
                    offenders.append(module)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
            if module == "opencollab" and any(alias.name == "sdk" for alias in node.names):
                offenders.append("opencollab.sdk")
            if module == "opencollab.sdk" or module.startswith("opencollab.sdk."):
                offenders.append(module)
    return offenders


def _environment_subclasses(tree: ast.AST) -> list[str]:
    aliases = {"Environment", "ExecutionEnvironment"}
    module_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "opencollab.environments",
            "opencollab_eval.engine.environment",
        }:
            aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in {"Environment", "ExecutionEnvironment"}
            )
        elif isinstance(node, ast.Import):
            module_aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in {
                    "opencollab.environments",
                    "opencollab_eval.engine.environment",
                }
            )

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id in aliases:
                offenders.append(node.name)
            elif (
                isinstance(base, ast.Attribute)
                and base.attr in {"Environment", "ExecutionEnvironment"}
                and isinstance(base.value, ast.Name)
                and base.value.id in module_aliases
            ):
                offenders.append(node.name)
    return offenders


def _sys_path_mutation_lines(tree: ast.AST) -> list[int]:
    mutations: list[int] = []

    def is_sys_path(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "path"
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
        )

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"append", "extend", "insert"}
            and is_sys_path(node.func.value)
        ):
            mutations.append(node.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                is_sys_path(target)
                or (isinstance(target, ast.Subscript) and is_sys_path(target.value))
                for target in targets
            ):
                mutations.append(node.lineno)
    return mutations


def _python_subprocess_file_entry_lines(tree: ast.AST) -> list[int]:
    offenders: list[int] = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Attribute)
            or node.func.attr not in {"Popen", "run"}
            or not node.args
            or not isinstance(node.args[0], (ast.List, ast.Tuple))
        ):
            continue
        command = node.args[0].elts
        if len(command) < 2:
            continue
        executable = command[0]
        entry = command[1]
        is_current_python = (
            isinstance(executable, ast.Attribute)
            and executable.attr == "executable"
            and isinstance(executable.value, ast.Name)
            and executable.value.id == "sys"
        )
        is_installed_entry = isinstance(entry, ast.Constant) and entry.value in {"-c", "-m"}
        if is_current_python and not is_installed_entry:
            offenders.append(node.lineno)
    return offenders


def test_eval_code_uses_only_documented_opencollab_public_modules() -> None:
    offenders: list[str] = []
    paths = (*sorted(_SOURCE.rglob("*.py")), *sorted(_TEST_ROOT.rglob("*.py")))
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(f"{path}: {module}" for module in _forbidden_opencollab_imports(tree))
    assert offenders == []


def test_retired_opencollab_sdk_does_not_return() -> None:
    offenders: list[str] = []
    paths = (*sorted(_SOURCE.rglob("*.py")), *sorted(_TEST_ROOT.rglob("*.py")))
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(f"{path}: {module}" for module in _retired_sdk_imports(tree))
    assert offenders == []


def test_test_fakes_implement_environment_protocol_structurally() -> None:
    offenders: list[str] = []
    for path in sorted(_TEST_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(
            f"{path}: {name}"
            for name in _environment_subclasses(tree)
        )
    assert offenders == []


def test_retired_parallel_solver_adapter_does_not_return() -> None:
    assert not (_SOURCE / "solvers" / "__init__.py").exists()
    assert not (_SOURCE / "solvers" / "opencollab.py").exists()
    assert not (_SOURCE / "engine" / "eval_adapter" / "workspace.py").exists()
    contracts = (_SOURCE / "contracts" / "models.py").read_text(encoding="utf-8")
    assert "class SolverRun" not in contracts
    assert "class PreparedWorkspace" not in contracts
    assert "class SolverBudget" not in contracts


def test_single_agent_prompt_builder_does_not_read_sealed_task_fields() -> None:
    path = _SOURCE / "generation" / "gen_prediction_agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    build_task = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "build_task"
    )
    string_literals = {
        node.value
        for node in ast.walk(build_task)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    sealed_fields = {
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
        "base_commit",
        "instance_id",
        "reference_patch",
        "test_patch",
    }
    assert string_literals.isdisjoint(sealed_fields)


def test_public_import_boundary_accepts_documented_import_forms() -> None:
    allowed = ast.parse(
        "import opencollab\n"
        "import opencollab.environments\n"
        "import opencollab.tools\n"
        "import opencollab.workflows\n"
        "from opencollab import OpenCollab, RunError, RunResult, workflow\n"
        "from opencollab.environments import Environment, attach_container\n"
        "from opencollab.tools import Tool, VerificationTool, builtin_tools\n"
        "from opencollab.workflows import WorkflowContext\n"
    )

    assert _forbidden_opencollab_imports(allowed) == []
    assert _retired_sdk_imports(allowed) == []


def test_public_import_allowlist_matches_opencollab_exports() -> None:
    for module_name, names in _PUBLIC_NAMES.items():
        module = importlib.import_module(module_name)
        assert names <= frozenset(module.__all__)


def test_public_import_boundary_rejects_internal_and_retired_import_forms() -> None:
    forbidden = ast.parse(
        "import opencollab.adapters\n"
        "import opencollab.application\n"
        "import opencollab.bootstrap\n"
        "import opencollab.domain\n"
        "import opencollab.harness\n"
        "import opencollab.sdk\n"
        "import opencollab.sdk.client\n"
        "from opencollab import adapters, application, bootstrap, domain, harness, sdk\n"
        "from opencollab.adapters import tools\n"
        "from opencollab.sdk import OpenCollab\n"
        "from opencollab.sdk.experimental import workflow\n"
        "from opencollab.tools import _private_tool\n"
    )

    assert _forbidden_opencollab_imports(forbidden) == [
        "opencollab.adapters",
        "opencollab.application",
        "opencollab.bootstrap",
        "opencollab.domain",
        "opencollab.harness",
        "opencollab.sdk",
        "opencollab.sdk.client",
        "opencollab.adapters",
        "opencollab.application",
        "opencollab.bootstrap",
        "opencollab.domain",
        "opencollab.harness",
        "opencollab.sdk",
        "opencollab.adapters",
        "opencollab.sdk",
        "opencollab.sdk.experimental",
        "opencollab.tools._private_tool",
    ]
    assert _retired_sdk_imports(forbidden) == [
        "opencollab.sdk",
        "opencollab.sdk.client",
        "opencollab.sdk",
        "opencollab.sdk",
        "opencollab.sdk.experimental",
    ]


def test_environment_protocol_subclass_detector_handles_aliases() -> None:
    direct = ast.parse(
        "from opencollab.environments import Environment\n"
        "class Fake(Environment):\n"
        "    pass\n"
    )
    aliased = ast.parse(
        "from opencollab.environments import Environment as EnvProtocol\n"
        "class Fake(EnvProtocol):\n"
        "    pass\n"
    )
    evaluation_owned = ast.parse(
        "from opencollab_eval.engine.environment import ExecutionEnvironment\n"
        "class Fake(ExecutionEnvironment):\n"
        "    pass\n"
    )

    assert _environment_subclasses(direct) == ["Fake"]
    assert _environment_subclasses(aliased) == ["Fake"]
    assert _environment_subclasses(evaluation_owned) == ["Fake"]


def test_production_path_mutation_is_limited_to_remote_runtime_bootstrap() -> None:
    mutations: list[Path] = []
    for path in sorted(_SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        mutations.extend(path.relative_to(_SOURCE) for _line in _sys_path_mutation_lines(tree))

    assert mutations == [Path("engine/swe_v1_remote_state.py")]
    remote_state = (_SOURCE / mutations[0]).read_text(encoding="utf-8")
    assert 'package_root = remote_repo / "src"' in remote_state


def test_test_code_never_mutates_sys_path() -> None:
    mutations: list[str] = []
    for path in sorted(_TEST_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        mutations.extend(f"{path}:{line}" for line in _sys_path_mutation_lines(tree))
    assert mutations == []


def test_python_subprocesses_use_code_or_installed_module_entries() -> None:
    offenders: list[str] = []
    for path in sorted(_TEST_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(
            f"{path}:{line}" for line in _python_subprocess_file_entry_lines(tree)
        )
    assert offenders == []


def test_path_mutation_detector_ignores_pure_string_fixture() -> None:
    fixture = ast.parse('FORBIDDEN_PATTERN = "sys.path.insert(0, value)"')
    executable = ast.parse("import sys\nsys.path.insert(0, value)\n")

    assert _sys_path_mutation_lines(fixture) == []
    assert _sys_path_mutation_lines(executable) == [2]


def test_subprocess_entry_detector_rejects_source_file_execution() -> None:
    source_file = ast.parse("subprocess.run([sys.executable, str(script)])")
    installed_module = ast.parse(
        'subprocess.run([sys.executable, "-m", "opencollab_eval.commands.tool"])'
    )

    assert _python_subprocess_file_entry_lines(source_file) == [1]
    assert _python_subprocess_file_entry_lines(installed_module) == []


def test_evaluation_strategy_is_owned_by_eval_package() -> None:
    fact_sheet = (_SOURCE / "workflows" / "_fact_sheet.py").read_text(encoding="utf-8")
    analyst_runtime = (_SOURCE / "workflows" / "_analyst_solve_runtime.py").read_text(
        encoding="utf-8"
    )

    assert "opencollab" not in fact_sheet
    assert "from ._fact_sheet import" in analyst_runtime
    assert "from ._public_api import format_findings_report" in analyst_runtime


def test_single_agent_generation_delegates_lifecycle_to_public_facade() -> None:
    path = _SOURCE / "generation" / "gen_prediction_agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden_modules = {
        "opencollab.adapters",
        "opencollab.application",
        "opencollab.bootstrap",
        "opencollab.domain",
        "opencollab.sdk",
    }
    forbidden_names = {
        "Agent",
        "AgentRunRequest",
        "OpenCollabRuntime",
        "SessionPhase",
        "Tracer",
        "abandon_on_timeout",
        "build_session",
    }
    imported_modules: set[str] = set()
    imported_names: set[str] = set()
    lifecycle_calls: list[str] = []
    helper_definitions: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
            imported_names.update(alias.name for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"add_user_message", "run_loop"}
        ):
            lifecycle_calls.append(node.func.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_quiesce_agent_tasks":
            helper_definitions.append(node.name)

    assert forbidden_modules.isdisjoint(imported_modules)
    assert forbidden_names.isdisjoint(imported_names)
    assert lifecycle_calls == []
    assert helper_definitions == []
    assert {"OpenCollab", "RunResult", "attach_container", "builtin_tools"} <= imported_names


def test_workflow_documentation_uses_packaged_layout() -> None:
    readme = (_SOURCE / "workflows" / "README.md").read_text(encoding="utf-8")
    stale_layout_fragments = (
        "swebench/gen_prediction_workflow.py",
        "OPENCOLLAB_WORKFLOWS_DIR",
        "`opencollab workflow",
        "opencollab/.venv",
        "`application/workflow.py`",
        "`opencollab/adapters/tools/`",
        "`workflows/` relative to the working directory",
    )

    assert [fragment for fragment in stale_layout_fragments if fragment in readme] == []
