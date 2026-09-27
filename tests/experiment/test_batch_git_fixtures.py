"""Batch tests exercise revision validation against their own Git objects."""

from __future__ import annotations

from pathlib import Path

from opencollab_eval.commands import batch as batch_cli
from opencollab_eval.experiment.batch_spec import load_spec
from tests.experiment.batch_support import (
    PIN_EVAL,
    _spec_text,
)
from tests.experiment.batch_support import (
    experiment as experiment,
)
from tests.experiment.batch_support import (
    oc_repo as oc_repo,
)


def test_batch_fixture_validates_both_generated_repository_commits(experiment: dict) -> None:
    spec = load_spec(experiment["spec"])
    assert spec.pins == {"opencollab": experiment["sha"], "opencollab_eval": experiment["eval_sha"]}
    assert batch_cli.commit_exists(experiment["repo"], experiment["sha"])
    assert batch_cli.commit_exists(experiment["eval_repo"], experiment["eval_sha"])
    assert (
        batch_cli.main(
            ["--experiment-dir", str(experiment["dir"]), "plan", str(experiment["spec"])],
            remote_factory=lambda _host: None,
        )
        == 0
    )


def test_historical_pin_missing_from_fixture_repository_is_rejected(experiment: dict, capsys) -> None:
    assert not batch_cli.commit_exists(experiment["eval_repo"], PIN_EVAL)
    path = Path(experiment["dir"]) / "batches" / "missing-history.yaml"
    path.write_text(_spec_text(experiment, experiment["eval_sha"], PIN_EVAL), encoding="utf-8")
    assert (
        batch_cli.main(
            ["--experiment-dir", str(experiment["dir"]), "plan", str(path)],
            remote_factory=lambda _host: None,
        )
        == 2
    )
    assert "not a commit" in capsys.readouterr().err
