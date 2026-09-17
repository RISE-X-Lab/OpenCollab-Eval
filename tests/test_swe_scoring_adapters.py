from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import typing

import pytest
from swe_v1_prolite_runner_test_support import _remote_namespace, _seed_remote_completed_generation

from opencollab_eval.commands import swe_g11_parallel_runner as parallel
from opencollab_eval.commands import swe_v1_prolite_runner as runner
from opencollab_eval.commands.package_runtime import package_runtime
from opencollab_eval.engine import swe_eval_scoring_adapters as adapters
from opencollab_eval.engine import swe_v1_remote_evaluation as evaluation
from opencollab_eval.engine import swe_v1_remote_state as state

TASK = "instance_owner__repo-adapter"


def _row():
    return {
        "instance_id": TASK, "problem_statement": "Keep the public behavior",
        "requirements": "Keep the assertions", "interface": "PublicFeature",
        "dockerhub_tag": "fake.image", "repo_language": "go",
        "fail_to_pass": ["pkg/feature_test.go::TestFeature"], "pass_to_pass": [],
        "test_patch": "original test fixture", "nested": {"value": 1},
    }


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.delenv(adapters.REGISTRY_ENV, raising=False)
    external = tmp_path / "external-adapters"
    external.mkdir()
    (external / "adapter.py").write_text(
        "import copy\nfrom pathlib import Path\n"
        f"INSTANCE_IDS = {{{TASK!r}}}\nADAPTER_ID = 'public-fixture-test'\n"
        "def adapt(row):\n"
        "    with Path(__file__).with_name('calls.txt').open('a') as log: log.write('called\\n')\n"
        "    result = copy.deepcopy(row)\n    result['test_patch'] = 'adapted test fixture'\n"
        "    return result, {'schema': 'opencollab.scoring_adapter_receipt.v1', "
        "'adapter_id': ADAPTER_ID, 'changed_fields': ['test_patch'], "
        "'solver_input_unchanged': True, 'gold_production_code_added': False}\n"
    )
    path = external / "registry.json"
    path.write_text(json.dumps({
        "schema": adapters.REGISTRY_SCHEMA,
        "entries": [{"instance_id": TASK, "adapter_id": "public-fixture-test",
                     "module": "adapter.py", "allowed_changed_fields": ["test_patch"]}],
    }))
    return path


def _calls(registry):
    path = registry.with_name("calls.txt")
    return len(path.read_text().splitlines()) if path.exists() else 0


def test_preparation_is_once_and_does_not_trust_dataset_receipt_fields(registry):
    row = _row()
    row["_scoring_adapter_receipt"] = {"applied": True}
    original = copy.deepcopy(row)
    prepared = adapters.prepare_scoring_row(row, registry)
    assert adapters.prepare_scoring_row(prepared, registry) is prepared
    assert _calls(registry) == 1
    assert row == original
    assert prepared["test_patch"] == "adapted test fixture"
    assert prepared.scoring_adapter_receipt["registry"] == str(registry)
    assert prepared.scoring_adapter_receipt["module"] == str(registry.with_name("adapter.py"))
    assert prepared.scoring_adapter_receipt["original_instance_sha256"] == hashlib.sha256(
        json.dumps(original, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert set(prepared) == set(row)
    prepared["nested"]["value"] = 2
    assert row["nested"]["value"] == 1


def test_unconfigured_scoring_preserves_a_separate_original_copy(monkeypatch):
    monkeypatch.delenv(adapters.REGISTRY_ENV, raising=False)
    row = _row()
    prepared = adapters.prepare_scoring_row(row)
    assert prepared == row and prepared is not row
    assert prepared.scoring_adapter_receipt["applied"] is False
    assert prepared.scoring_adapter_receipt["registry"] is None


def test_external_dataclass_modules_keep_their_own_import_namespace(registry):
    path = registry.with_name("adapter.py")
    path.write_text(
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "@dataclass\nclass Node:\n    child: Node | None = None\n" + path.read_text()
    )
    adapters.adapt_instance(_row(), registry)
    first = next(module for module in sys.modules.values() if getattr(module, "__file__", None) == str(path))
    adapters.adapt_instance(_row(), registry)
    modules = [module for module in sys.modules.values() if getattr(module, "__file__", None) == str(path)]
    assert len(modules) == 2
    assert typing.get_type_hints(first.Node)["child"].__args__[0] is first.Node


def test_failed_module_import_removes_its_registration(registry):
    before = {name for name in sys.modules if name.startswith("opencollab_scoring_adapter_")}
    registry.with_name("adapter.py").write_text("raise RuntimeError('adapter import failed')\n")
    with pytest.raises(RuntimeError, match="adapter import failed"):
        adapters.adapt_instance(_row(), registry)
    assert {name for name in sys.modules if name.startswith("opencollab_scoring_adapter_")} == before


def test_executed_official_report_and_inputs_retain_adapter_provenance(registry, tmp_path, monkeypatch):
    from test_swe_v1_prolite_runner_evaluation_legacy import (
        test_remote_runner_eval_only_uses_existing_patch_without_starting_generation,
    )

    task = "instance_owner__repo-eval-only"
    data = json.loads(registry.read_text())
    data["entries"][0]["instance_id"] = task
    registry.write_text(json.dumps(data))
    module = registry.with_name("adapter.py")
    module.write_text(module.read_text().replace(TASK, task))
    monkeypatch.setenv(adapters.REGISTRY_ENV, str(registry))
    test_remote_runner_eval_only_uses_existing_patch_without_starting_generation(tmp_path)
    eval_dir = tmp_path / "run" / task / "official_eval_fresh"
    official = json.loads((eval_dir / "reports" / task / "report.json").read_text())[task]
    summary = json.loads((eval_dir / "summary.json").read_text())
    receipt = json.loads((eval_dir / "input/scoring_adapter_receipt.json").read_text())
    assert receipt == official["scoring_adapter"] == summary["scoring_adapter"]
    assert receipt["applied"] is True and receipt["registry"] == str(registry)
    assert (eval_dir / "input/test.patch").read_text() == "adapted test fixture"
    assert (eval_dir / "input/model.patch").read_text().endswith("-a\n+b\n")
    assert _calls(registry) == 1


@pytest.mark.parametrize("invalid", ["missing", "malformed", "duplicate", "module", "identity", "fields"])
def test_explicit_invalid_adapter_configuration_raises(registry, invalid):
    data = json.loads(registry.read_text())
    if invalid == "missing":
        registry.unlink()
    elif invalid == "malformed":
        registry.write_text("{")
    elif invalid == "duplicate":
        data["entries"].append(copy.deepcopy(data["entries"][0]))
        registry.write_text(json.dumps(data))
    elif invalid == "module":
        registry.with_name("adapter.py").unlink()
    elif invalid == "identity":
        data["entries"][0]["adapter_id"] = "different-adapter"
        registry.write_text(json.dumps(data))
    elif invalid == "fields":
        data["entries"][0]["allowed_changed_fields"] = []
        registry.write_text(json.dumps(data))
    with pytest.raises((ValueError, FileNotFoundError)):
        adapters.prepare_scoring_row(_row(), registry)


def _plan_probe(namespace, observed):
    def validated(**kwargs):
        row = kwargs["row"]
        assert isinstance(row, adapters.PreparedScoringRow)
        assert row["test_patch"] == "adapted test fixture"
        selection = kwargs["patch_selection"]
        if selection is not None:
            assert kwargs["eval_spec_sha256"] == selection["eval_spec_sha256"]
        observed.append(kwargs)
        return {"ready": False, "result": {
            "status": "technical_eval_failed", "executed": False,
            "summary": {"resolved": False, "technical_reasons": ["test_plan_probe"]},
        }}
    namespace["validated_eval_patch"] = validated


def test_main_retry_and_once_route_generation_and_plans_with_one_adapter(registry, tmp_path):
    ns = _remote_namespace(tmp_path, scoring_adapter_registry=str(registry), checkpoint_interval=0)
    _seed_remote_completed_generation(ns, TASK)
    row = _row()
    original = copy.deepcopy(row)
    _, prediction, metric, _ = ns["generation_done_for_mode"](ns["base_run_dir"] / TASK, TASK, eval_only=False)
    original_selection = ns["verified_plan_patch_selection"](row, prediction, metric, ns["eval_timeout"])
    ns["dataset_path"].parent.mkdir(parents=True, exist_ok=True)
    ns["dataset_path"].write_text(json.dumps(row) + "\n")
    ns["http_health"] = lambda *args, **kwargs: {"ok": True}
    generated = []
    isolated = []
    observed = []

    def generation(actual):
        generated.append(actual)
        assert actual == original and not isinstance(actual, adapters.PreparedScoringRow)
        return {"status": "generation_done", "oc_failure": False, "technical_failure": False}

    def isolation(selected):
        isolated.extend(selected)
        assert selected[0]["test_patch"] == "adapted test fixture"
        return None

    ns["generation_for_task"] = generation
    ns["prepare_eval_only_candidate_isolation"] = isolation
    _plan_probe(ns, observed)
    assert ns["main"]() == 1
    assert len(generated) == len(isolated) == len(observed) == _calls(registry) == 1
    assert generated[0] == original
    assert observed[0]["row"] is isolated[0]
    assert observed[0]["eval_spec_sha256"] != original_selection["eval_spec_sha256"]
    summary = json.loads((ns["base_run_dir"] / "summary.json").read_text())
    assert summary["scoring_adapter_registry"] == str(registry)
    assert summary["rows"][0]["scoring_adapter"]["applied"] is True


@pytest.mark.parametrize("invalid", ["missing", "identity"])
def test_main_invalid_registry_stops_before_generation_or_old_scoring(registry, tmp_path, invalid):
    ns = _remote_namespace(tmp_path, scoring_adapter_registry=str(registry), checkpoint_interval=0)
    ns["dataset_path"].parent.mkdir(parents=True, exist_ok=True)
    ns["dataset_path"].write_text(json.dumps(_row()) + "\n")
    ns["http_health"] = lambda *args, **kwargs: {"ok": True}
    calls = []
    ns["generation_for_task"] = lambda row: calls.append("generation")
    ns["eval_for_task"] = lambda row: calls.append("eval")
    if invalid == "missing":
        registry.unlink()
    else:
        data = json.loads(registry.read_text())
        data["entries"][0]["adapter_id"] = "different-adapter"
        registry.write_text(json.dumps(data))
    with pytest.raises((FileNotFoundError, ValueError)):
        ns["main"]()
    assert calls == []


@pytest.mark.parametrize("entry", ["eval_for_task", "eval_for_task_once"])
def test_direct_scoring_entries_prepare_before_actual_plans(registry, tmp_path, entry):
    ns = _remote_namespace(tmp_path, scoring_adapter_registry=str(registry))
    _seed_remote_completed_generation(ns, TASK)
    observed = []
    _plan_probe(ns, observed)
    result = ns[entry](_row())
    assert result["status"] == "technical_eval_failed"
    assert len(observed) == _calls(registry) == 1
    assert observed[0]["row"].scoring_adapter_receipt["applied"] is True


def test_direct_import_reads_the_active_registry_configuration(registry, monkeypatch):
    monkeypatch.setattr(state, "cfg", {"scoring_adapter_registry": str(registry)})
    captured = []
    monkeypatch.setattr(evaluation, "eval_for_task_with_retries", lambda row, *args: captured.append(row) or {})
    evaluation.eval_for_task(_row())
    assert captured[0].scoring_adapter_receipt["applied"] is True
    assert _calls(registry) == 1


def test_parallel_registry_is_an_explicit_child_cli_option(registry):
    from test_swe_g11_parallel_runner import _args

    config = parallel.resolve_config(_args(scoring_adapter_registry=str(registry)))
    command = parallel.task_command(config, 51)
    assert command[command.index("--scoring-adapter-registry") + 1] == str(registry)
    assert all(not item.startswith(adapters.REGISTRY_ENV + "=") for item in command)


PACKAGED_PROBE = '''
import json, pathlib, sys
from opencollab_eval.commands import swe_v1_prolite_runner as cli
from opencollab_eval.engine.swe_v1_remote_runner import install_into
from opencollab_eval.engine.swe_eval_scoring_adapters import prepare_scoring_row
registry, transport, row_json, source = sys.argv[1:]
class Captured(BaseException): pass
def execute(args):
    cli._controller.get_proxy_token = lambda path: "test-token"
    payload = cli._controller._remote_payload(args, owner_nonce="a" * 32,
        invocation_id="b" * 32, runtime_tree_sha256="c" * 64,
        remote_proxy_base_url="http://127.0.0.1:1")
    payload["token"] = "test-token"
    assert payload["scoring_adapter_registry"] == registry
    assert "OPENCOLLAB_EVAL_SCORING_ADAPTER_REGISTRY" not in payload["workflow_env"]
    namespace = {}
    install_into(namespace, payload)
    row = json.loads(row_json)
    prepared = prepare_scoring_row(row, namespace["resolve_scoring_adapter_registry"]())
    namespace["eval_for_task_once"](prepared)
    assert prepared["test_patch"] == "adapted test fixture"
    assert pathlib.Path(registry).with_name("calls.txt").read_text().splitlines() == ["called"]
    assert row["test_patch"] == "original test fixture"
    print(json.dumps({"registry": namespace["cfg"]["scoring_adapter_registry"],
        "module": prepare_scoring_row.__module__, "applied": prepared.scoring_adapter_receipt["applied"]}))
    raise Captured()
cli.run_remote = execute
try:
    argv=["--host", "worker", "--runner-transport", transport,
        "--remote-root", str(pathlib.Path.cwd() / "worker-root"),
        "--base-run-dir", str(pathlib.Path.cwd() / "run"), "--model-name", "test",
        "--session-prefix", "test", "--image-repository", "example.test/images",
        "--remote-proxy-base-url", "http://127.0.0.1:1", "--no-ensure-remote-proxy",
        "--dry-run"]
    if source == "cli": argv.extend(["--scoring-adapter-registry", registry])
    cli.main(argv=argv)
except Captured: pass
'''


@pytest.mark.parametrize("packager,transport,source", [("package", "local", "env"), ("sync", "ssh", "cli")])
def test_actual_runtime_package_carries_cli_config_and_scoring_entry(
    registry, tmp_path, monkeypatch, packager, transport, source,
):
    runtime = tmp_path / "packaged"
    if packager == "package":
        package_runtime(runtime)
    else:
        runtime.mkdir()

        def capture(command, **kwargs):
            if command[0] == "rsync":
                with tarfile.open(command[-2], "r:gz") as archive:
                    archive.extractall(runtime, filter="data")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(runner, "run_checked", capture)
        runner.sync_runtime(ssh_command=["ssh"], host="worker", remote_runtime_repo="/worker/runtime")
    manifest = json.loads((runtime / "runtime-manifest.json").read_text())
    assert "src/opencollab_eval/engine/swe_eval_scoring_adapters.py" in manifest["archive_members"]
    assert not list(runtime.rglob("registry.json"))
    completed = subprocess.run(
        [sys.executable, "-c", PACKAGED_PROBE, str(registry), transport, json.dumps(_row()), source],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(runtime / "src"),
                          **({adapters.REGISTRY_ENV: str(registry)} if source == "env" else {})},
        text=True, capture_output=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["applied"] is True
    assert _calls(registry) == 1
