"""Launch records distinguish preparation failures from dispatched drivers."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from opencollab_eval.commands import batch as cli
from opencollab_eval.commands import batch_state
from opencollab_eval.commands.batch_reporting import _check_recorded_cell_models
from opencollab_eval.experiment.batch_spec import SpecError
from tests.experiment.batch_support import FakeRemote, _facts, _spec_text
from tests.experiment.batch_support import experiment as experiment
from tests.experiment.batch_support import oc_repo as oc_repo


def _args(experiment: dict, spec: Path | None = None) -> list[str]:
    return ["--experiment-dir", str(experiment["dir"]), "launch", str(spec or experiment["spec"])]


def _record(experiment: dict, name: str = "t1") -> dict:
    path = Path(experiment["dir"]).parent / "batches" / f"{name}.launch" / "batch.json"
    return json.loads(path.read_text())


def _other_model(experiment: dict) -> str:
    return _facts(experiment).replace("MODEL\tdeepseek-v4-flash", "MODEL\tother-model")


class PreparationFails(FakeRemote):
    def __init__(self, facts: str, phase: str) -> None:
        super().__init__(facts)
        self.phase = phase

    def copy_to(self, local_paths, remote_dir: str) -> None:
        phase = "metadata" if str(local_paths[0]).endswith("batch.json") else "input"
        if self.phase == phase:
            raise OSError(f"failed {phase}")
        super().copy_to(local_paths, remote_dir)

    def run(self, script: str, timeout: float = 0) -> str:
        if self.phase == "mkdir" and script.startswith("mkdir -p "):
            raise cli.RemoteError("failed mkdir")
        return super().run(script, timeout)


@pytest.mark.parametrize("phase", ["input", "mkdir", "metadata"])
def test_preparation_failure_releases_identity_and_preserves_history(experiment: dict, phase: str) -> None:
    failed = PreparationFails(_facts(experiment), phase)
    assert cli.main(_args(experiment), remote_factory=lambda host: failed) == 2
    assert not any("setsid nohup env" in script for script in failed.scripts)
    first = _record(experiment)["launches"]
    assert len(first) == 1 and first[0]["state"] == batch_state.NOT_STARTED
    assert first[0]["model_identity"]["model"] == "deepseek-v4-flash"

    changed = FakeRemote(_other_model(experiment))
    assert cli.main(_args(experiment), remote_factory=lambda host: changed) == 0
    launches = _record(experiment)["launches"]
    assert [entry["state"] for entry in launches] == [batch_state.NOT_STARTED, batch_state.STARTED]
    assert launches[1]["model_identity"]["model"] == "other-model"
    _check_recorded_cell_models(cli.Batch(Path(experiment["spec"]), Path(experiment["dir"])))
    assert cli.main(_args(experiment), remote_factory=lambda host: FakeRemote(_facts(experiment))) == 2
    assert cli.main(_args(experiment), remote_factory=lambda host: FakeRemote(_other_model(experiment))) == 0


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_interrupted_preparation_releases_identity(experiment: dict, interruption: type[BaseException]) -> None:
    class InterruptedUpload(FakeRemote):
        def copy_to(self, local_paths, remote_dir: str) -> None:
            raise interruption("stopped during upload")

    with pytest.raises(interruption, match="stopped during upload"):
        cli.main(_args(experiment), remote_factory=lambda host: InterruptedUpload(_facts(experiment)))
    assert _record(experiment)["launches"][0]["state"] == batch_state.NOT_STARTED
    assert cli.main(_args(experiment), remote_factory=lambda host: FakeRemote(_other_model(experiment))) == 0


def test_failed_state_write_preserves_original_preparation_error(experiment: dict, monkeypatch, capsys) -> None:
    original = batch_state.set_launch_state

    def failing_update(batch, index: int, state: str) -> None:
        if state == batch_state.NOT_STARTED:
            raise OSError("state disk unavailable")
        original(batch, index, state)

    monkeypatch.setattr(batch_state, "set_launch_state", failing_update)
    with pytest.raises(OSError, match="failed input") as caught:
        batch = cli.Batch(Path(experiment["spec"]), Path(experiment["dir"]))
        cli.cmd_launch(batch, PreparationFails(_facts(experiment), "input"), None)
    assert any("state disk unavailable" in note for note in caught.value.__notes__)
    assert _record(experiment)["launches"][0]["state"] == batch_state.PREPARING
    assert cli.main(_args(experiment), remote_factory=lambda host: PreparationFails(_facts(experiment), "input")) == 2
    stderr = capsys.readouterr().err
    assert "error: failed input" in stderr
    assert "could not record confirmed non-start" in stderr


def test_local_script_construction_precedes_model_reservation(experiment: dict, monkeypatch) -> None:
    def cannot_construct(*args, **kwargs) -> str:
        raise OSError("invalid local command")

    monkeypatch.setattr(cli, "launch_script", cannot_construct)
    assert cli.main(_args(experiment), remote_factory=lambda host: FakeRemote(_facts(experiment))) == 2
    path = Path(experiment["dir"]).parent / "batches" / "t1.launch" / "batch.json"
    assert not path.exists()


class DispatchFails(FakeRemote):
    def __init__(self, facts: str, *, empty: bool) -> None:
        super().__init__(facts)
        self.empty = empty

    def run(self, script: str, timeout: float = 0) -> str:
        if "setsid nohup env" in script:
            self.scripts.append(script)
            if self.empty:
                return ""
            raise cli.RemoteError("lost launch reply")
        return super().run(script, timeout)


@pytest.mark.parametrize("empty", [True, False])
def test_dispatched_launch_without_confirmation_retains_identity(experiment: dict, empty: bool) -> None:
    remote = DispatchFails(_facts(experiment), empty=empty)
    assert cli.main(_args(experiment), remote_factory=lambda host: remote) == (1 if empty else 2)
    assert _record(experiment)["launches"][0]["state"] == batch_state.START_UNKNOWN
    changed = FakeRemote(_other_model(experiment))
    assert cli.main(_args(experiment), remote_factory=lambda host: changed) == 2
    assert not any("setsid nohup env" in script for script in changed.scripts)
    assert cli.main(_args(experiment), remote_factory=lambda host: FakeRemote(_facts(experiment))) == 0
    assert [entry["state"] for entry in _record(experiment)["launches"]] == [
        batch_state.START_UNKNOWN,
        batch_state.STARTED,
    ]


def test_interrupted_dispatch_remains_unknown(experiment: dict) -> None:
    class InterruptedDispatch(FakeRemote):
        def run(self, script: str, timeout: float = 0) -> str:
            if "setsid nohup env" in script:
                raise KeyboardInterrupt("lost caller")
            return super().run(script, timeout)

    with pytest.raises(KeyboardInterrupt, match="lost caller"):
        cli.main(_args(experiment), remote_factory=lambda host: InterruptedDispatch(_facts(experiment)))
    assert _record(experiment)["launches"][0]["state"] == batch_state.START_UNKNOWN
    assert cli.main(_args(experiment), remote_factory=lambda host: FakeRemote(_other_model(experiment))) == 2


def test_legacy_launch_keeps_model_claim_after_failed_preparation(experiment: dict) -> None:
    assert cli.main(_args(experiment), remote_factory=lambda host: FakeRemote(_facts(experiment))) == 0
    path = Path(experiment["dir"]).parent / "batches" / "t1.launch" / "batch.json"
    record = _record(experiment)
    record["launches"][0].pop("state")
    record["launches"][0].pop("model_identity")
    path.write_text(json.dumps(record))
    assert cli.main(_args(experiment), remote_factory=lambda host: PreparationFails(_facts(experiment), "input")) == 2
    assert cli.main(_args(experiment), remote_factory=lambda host: FakeRemote(_other_model(experiment))) == 2
    assert cli.main(_args(experiment), remote_factory=lambda host: FakeRemote(_facts(experiment))) == 0


def _retry(experiment: dict) -> Path:
    path = Path(experiment["dir"]) / "batches" / "t1r.yaml"
    path.write_text(
        _spec_text(experiment, "name: t1", "name: t1r\nretry_of: t1").replace(
            "rows: {start: 1, stop: 2}", "rows: {start: 2, stop: 2}"
        )
    )
    return path


@pytest.mark.parametrize("relation", ["retry", "replacement"])
def test_family_cannot_change_model_while_preparation_is_active(experiment: dict, relation: str) -> None:
    assert cli.main(["--experiment-dir", str(experiment["dir"]), "plan", str(experiment["spec"])]) == 0
    if relation == "retry":
        sibling = _retry(experiment)
    else:
        sibling = Path(experiment["dir"]) / "batches" / "t1x.yaml"
        sibling.write_text(
            _spec_text(experiment, "name: t1", "name: t1x\nreplaces: {batch: t1, instance: b__b-2}")
            .replace("rows: {start: 1, stop: 2}", "rows: {start: 3, stop: 3}")
        )
    ready = threading.Event()
    release = threading.Event()

    class WaitingUpload(PreparationFails):
        def copy_to(self, local_paths, remote_dir: str) -> None:
            ready.set()
            assert release.wait(5)
            super().copy_to(local_paths, remote_dir)

    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            cli.main(_args(experiment), remote_factory=lambda host: WaitingUpload(_facts(experiment), "input"))
        )
    )
    thread.start()
    try:
        assert ready.wait(5)
        changed = FakeRemote(_other_model(experiment))
        assert cli.main(_args(experiment, sibling), remote_factory=lambda host: changed) == 2
        assert changed.copied == []
        # A sibling with the reserved identity may continue independently.
        assert cli.main(_args(experiment, sibling), remote_factory=lambda host: FakeRemote(_facts(experiment))) == 0
    finally:
        release.set()
        thread.join(5)
    assert result == [2]
    assert _record(experiment)["launches"][0]["state"] == batch_state.NOT_STARTED
    assert cli.main(_args(experiment), remote_factory=lambda host: FakeRemote(_other_model(experiment))) == 2


def test_same_name_waits_for_failed_preparation_then_can_change_model(experiment: dict) -> None:
    ready = threading.Event()
    release = threading.Event()
    result: list[int] = []

    class WaitingUpload(PreparationFails):
        def copy_to(self, local_paths, remote_dir: str) -> None:
            ready.set()
            assert release.wait(5)
            super().copy_to(local_paths, remote_dir)

    first = threading.Thread(
        target=lambda: result.append(
            cli.main(_args(experiment), remote_factory=lambda host: WaitingUpload(_facts(experiment), "input"))
        )
    )
    first.start()
    assert ready.wait(5)
    second = threading.Thread(
        target=lambda: result.append(
            cli.main(_args(experiment), remote_factory=lambda host: FakeRemote(_other_model(experiment)))
        )
    )
    second.start()
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert result == [2, 0]
    assert [entry["state"] for entry in _record(experiment)["launches"]] == [
        batch_state.NOT_STARTED,
        batch_state.STARTED,
    ]


def test_failed_attempts_do_not_create_report_model_conflict(experiment: dict) -> None:
    assert cli.main(_args(experiment), remote_factory=lambda host: PreparationFails(_facts(experiment), "input")) == 2
    record = _record(experiment)
    record["launches"].append(
        {"at": "2026-09-29T00:00:00+00:00", "state": batch_state.STARTED,
         "model_identity": {"model": "other-model", "provider": "openai", "base_url_sha256": "deadbeef"}}
    )
    path = Path(experiment["dir"]).parent / "batches" / "t1.launch" / "batch.json"
    path.write_text(json.dumps(record))
    _check_recorded_cell_models(cli.Batch(Path(experiment["spec"]), Path(experiment["dir"])))
    record["launches"][0]["state"] = batch_state.START_UNKNOWN
    path.write_text(json.dumps(record))
    with pytest.raises(SpecError, match="recorded paid model identity"):
        _check_recorded_cell_models(cli.Batch(Path(experiment["spec"]), Path(experiment["dir"])))
