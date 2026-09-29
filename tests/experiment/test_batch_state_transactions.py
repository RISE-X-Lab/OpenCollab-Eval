"""Batch records remain coherent across replacement, resume, and concurrent plans."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from opencollab_eval.commands import batch as batch_cli
from opencollab_eval.commands.batch_reporting import select_cell_rows
from opencollab_eval.experiment.batch_spec import SpecError, load_spec, spec_digest, spec_identity
from tests.experiment.batch_support import FakeRemote, _facts
from tests.experiment.batch_support import experiment as experiment
from tests.experiment.batch_support import oc_repo as oc_repo
from tests.experiment.test_batch_replacement import _metrics, _plan, _replacement_spec, _run


def _root(experiment: dict) -> Path:
    return Path(experiment["dir"]).parent / "batches"


def test_two_different_originals_cannot_use_the_same_replacement(experiment: dict, capsys) -> None:
    assert _plan(experiment, Path(experiment["spec"])) == 0
    first = _replacement_spec(experiment, "t1x", "rows: {start: 3, stop: 3}", "a__a-1")
    second = _replacement_spec(experiment, "t1y", "rows: {start: 3, stop: 3}", "b__b-2")
    assert _plan(experiment, first) == 0
    assert _plan(experiment, second) == 2
    assert "already used as a replacement" in capsys.readouterr().err


def test_report_rejects_historical_duplicate_replacement_arrivals(experiment: dict) -> None:
    assert _plan(experiment, Path(experiment["spec"])) == 0
    first = _replacement_spec(experiment, "t1x", "rows: {start: 3, stop: 3}", "a__a-1")
    second = _replacement_spec(experiment, "t1y", "rows: {start: 3, stop: 3}", "b__b-2")
    assert _plan(experiment, first) == 0
    historical = json.loads((_root(experiment) / "t1x.launch" / "batch.json").read_text())
    spec = load_spec(second)
    historical["spec"] = spec_identity(spec)
    historical["spec_digest"] = spec_digest(spec)
    historical["instances"]["file"] = spec.instances_file
    target = _root(experiment) / "t1y.launch" / "batch.json"
    target.parent.mkdir()
    target.write_text(json.dumps(historical))
    _metrics(_root(experiment) / "t1", [_run("a__a-1"), _run("b__b-2")])
    with pytest.raises(SpecError, match="stands in for two instances"):
        select_cell_rows(batch_cli.Batch(Path(experiment["spec"]), Path(experiment["dir"])))


def test_empty_replacement_metrics_keep_the_original_run(experiment: dict) -> None:
    assert _plan(experiment, Path(experiment["spec"])) == 0
    replacement = _replacement_spec(experiment, "t1x", "rows: {start: 3, stop: 3}", "b__b-2")
    assert _plan(experiment, replacement) == 0
    _metrics(_root(experiment) / "t1", [_run("a__a-1"), _run("b__b-2")])
    _metrics(_root(experiment) / "t1x", [])
    selection = select_cell_rows(batch_cli.Batch(Path(experiment["spec"]), Path(experiment["dir"])))
    assert [row.instance_id for row in selection.rows] == ["a__a-1", "b__b-2"]
    assert selection.excluded == [] and selection.missing == []


def test_wrong_replacement_metrics_cannot_change_the_cell(experiment: dict) -> None:
    assert _plan(experiment, Path(experiment["spec"])) == 0
    replacement = _replacement_spec(experiment, "t1x", "rows: {start: 3, stop: 3}", "b__b-2")
    assert _plan(experiment, replacement) == 0
    _metrics(_root(experiment) / "t1", [_run("a__a-1"), _run("b__b-2")])
    _metrics(_root(experiment) / "t1x", [_run("other__other-4")])
    with pytest.raises(SpecError, match="expected one observation"):
        select_cell_rows(batch_cli.Batch(Path(experiment["spec"]), Path(experiment["dir"])))


@pytest.mark.parametrize(
    ("original", "changed"),
    [
        ("MODEL\tdeepseek-v4-flash", "MODEL\tother-model"),
        ("PROVIDER\topenai", "PROVIDER\tother-provider"),
        ("BASE_URL_SHA\tdeadbeef", "BASE_URL_SHA\tother-address"),
    ],
)
def test_paid_model_identity_survives_preflight_and_rejects_changed_launch(
    experiment: dict, original: str, changed: str
) -> None:
    args = ["--experiment-dir", str(experiment["dir"])]
    spec = Path(experiment["spec"])
    facts = _facts(experiment)
    first = FakeRemote(facts)
    assert batch_cli.main([*args, "launch", str(spec)], remote_factory=lambda host: first) == 0
    path = _root(experiment) / "t1.launch" / "batch.json"
    before = path.read_bytes()
    record = json.loads(before)
    assert record["launches"][0]["model_identity"] == {
        "model": "deepseek-v4-flash",
        "provider": "openai",
        "base_url_sha256": "deadbeef",
    }
    drift = facts.replace(original, changed).replace("OUTDIR\tabsent", f"OUTDIR\t{spec_digest(load_spec(spec))}")
    second = FakeRemote(drift)
    assert batch_cli.main([*args, "preflight", str(spec)], remote_factory=lambda host: second) == 2
    assert path.read_bytes() == before
    assert batch_cli.main([*args, "launch", str(spec)], remote_factory=lambda host: second) == 2
    assert second.copied == [] and path.read_bytes() == before
    assert batch_cli.main([*args, "launch", str(spec)], remote_factory=lambda host: first) == 0
    after = json.loads(path.read_text())
    assert len(after["launches"]) == 2
    assert after["launches"][1]["model_identity"] == record["launches"][0]["model_identity"]
    assert "sk-" not in path.read_text()


def test_preflight_can_refresh_model_before_the_first_paid_launch(experiment: dict) -> None:
    args = ["--experiment-dir", str(experiment["dir"])]
    spec = Path(experiment["spec"])
    facts = _facts(experiment)
    newer = facts.replace("MODEL\tdeepseek-v4-flash", "MODEL\tother-model")
    assert batch_cli.main([*args, "preflight", str(spec)], remote_factory=lambda host: FakeRemote(facts)) == 0
    assert batch_cli.main([*args, "preflight", str(spec)], remote_factory=lambda host: FakeRemote(newer)) == 0
    path = _root(experiment) / "t1.launch" / "batch.json"
    assert json.loads(path.read_text())["host"]["model"] == "other-model"
    assert batch_cli.main([*args, "launch", str(spec)], remote_factory=lambda host: FakeRemote(newer)) == 0
    assert json.loads(path.read_text())["launches"][0]["model_identity"]["model"] == "other-model"


def test_unset_provider_uses_runtime_default_across_two_launches(experiment: dict) -> None:
    args = ["--experiment-dir", str(experiment["dir"])]
    spec = Path(experiment["spec"])
    no_provider = _facts(experiment).replace("PROVIDER\topenai", "PROVIDER\t")
    remote = FakeRemote(no_provider)
    assert batch_cli.main([*args, "launch", str(spec)], remote_factory=lambda host: remote) == 0
    assert batch_cli.main([*args, "launch", str(spec)], remote_factory=lambda host: remote) == 0
    record = json.loads((_root(experiment) / "t1.launch" / "batch.json").read_text())
    assert [entry["model_identity"]["provider"] for entry in record["launches"]] == ["openai", "openai"]
    assert record["host"]["provider"] == ""


def test_existing_launch_record_without_per_launch_identity_can_resume(experiment: dict) -> None:
    args = ["--experiment-dir", str(experiment["dir"])]
    spec = Path(experiment["spec"])
    facts = _facts(experiment).replace("PROVIDER\topenai", "PROVIDER\t")
    remote = FakeRemote(facts)
    assert batch_cli.main([*args, "launch", str(spec)], remote_factory=lambda host: remote) == 0
    path = _root(experiment) / "t1.launch" / "batch.json"
    older = json.loads(path.read_text())
    del older["launches"][0]["model_identity"]
    path.write_text(json.dumps(older))
    assert batch_cli.main([*args, "launch", str(spec)], remote_factory=lambda host: remote) == 0
    current = json.loads(path.read_text())
    assert len(current["launches"]) == 2
    assert current["launches"][1]["model_identity"]["provider"] == "openai"


def _use_real_eval_pin(experiment: dict, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = Path(batch_cli.__file__).resolve().parents[3]
    pin = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    spec = Path(experiment["spec"])
    spec.write_text(spec.read_text().replace(str(experiment["eval_sha"]), pin))
    monkeypatch.setattr(batch_cli, "REPO_ROOT", repo)
    return repo


def _two_process_actions(
    tmp_path: Path, repo: Path, experiment: dict, actions: tuple[tuple[str, Path, str], tuple[str, Path, str]]
) -> list[int]:
    gate = tmp_path / "start"
    code = (
        "import sys,time\n"
        "from pathlib import Path\n"
        "from opencollab_eval.commands.batch import main\n"
        "from tests.experiment.batch_support import FakeRemote\n"
        "Path(sys.argv[1]).write_text('ready')\n"
        "gate=Path(sys.argv[2])\n"
        "until=time.monotonic()+20\n"
        "while not gate.exists():\n"
        "    if time.monotonic()>until: raise SystemExit('start gate timed out')\n"
        "    time.sleep(.01)\n"
        "command=sys.argv[4]\n"
        "facts=Path(sys.argv[6]).read_text()\n"
        "factory=(lambda h:None) if command=='plan' else (lambda h:FakeRemote(facts))\n"
        "raise SystemExit(main(['--experiment-dir',sys.argv[3],command,sys.argv[5]],remote_factory=factory))\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(repo / "src"), str(repo), env.get("PYTHONPATH")]))
    processes = []
    for index, (command, spec, facts) in enumerate(actions):
        ready = tmp_path / f"ready-{index}"
        facts_file = tmp_path / f"facts-{index}.txt"
        facts_file.write_text(facts)
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    code,
                    str(ready),
                    str(gate),
                    str(experiment["dir"]),
                    command,
                    str(spec),
                    str(facts_file),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        )
    deadline = time.monotonic() + 20
    while not all((tmp_path / f"ready-{i}").exists() for i in range(2)):
        if time.monotonic() > deadline:
            for process in processes:
                process.kill()
            raise AssertionError("two plan processes did not reach the start gate")
        time.sleep(.01)
    gate.write_text("go")
    results = [process.communicate(timeout=30) for process in processes]
    codes = [process.returncode for process in processes]
    print(f"two process batch commands {[action[0] for action in actions]} returned {codes}")
    assert sorted(codes) == [0, 2], results
    return codes


def test_two_processes_cannot_plan_different_specs_under_one_name(
    experiment: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _use_real_eval_pin(experiment, monkeypatch)
    spec = Path(experiment["spec"])
    alternate = Path(experiment["dir"]) / "batches" / "t1-alternate.yaml"
    alternate.write_text(spec.read_text().replace("budget_per_seat: 2000000", "budget_per_seat: 1000000"))
    _two_process_actions(tmp_path, repo, experiment, (("plan", spec, ""), ("plan", alternate, "")))
    record = json.loads((_root(experiment) / "t1.launch" / "batch.json").read_text())
    assert record["spec"]["budget_per_seat"] in {1000000, 2000000}


def test_two_processes_cannot_claim_one_replacement_target(
    experiment: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _use_real_eval_pin(experiment, monkeypatch)
    assert _plan(experiment, Path(experiment["spec"])) == 0
    first = _replacement_spec(experiment, "t1x", "rows: {start: 3, stop: 3}", "b__b-2")
    second = _replacement_spec(experiment, "t1y", "rows: {start: 3, stop: 3}", "b__b-2")
    _two_process_actions(tmp_path, repo, experiment, (("plan", first, ""), ("plan", second, "")))
    records = list(_root(experiment).glob("t1*.launch/batch.json"))
    assert len(records) == 2  # original plus exactly one successful claimant


def test_plan_and_launch_processes_cannot_write_different_specs_under_one_name(
    experiment: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _use_real_eval_pin(experiment, monkeypatch)
    spec = Path(experiment["spec"])
    alternate = Path(experiment["dir"]) / "batches" / "t1-alternate.yaml"
    alternate.write_text(spec.read_text().replace("budget_per_seat: 2000000", "budget_per_seat: 1000000"))
    _two_process_actions(
        tmp_path,
        repo,
        experiment,
        (("launch", spec, _facts(experiment)), ("plan", alternate, "")),
    )
    record = json.loads((_root(experiment) / "t1.launch" / "batch.json").read_text())
    assert record["spec"]["budget_per_seat"] in {1000000, 2000000}
