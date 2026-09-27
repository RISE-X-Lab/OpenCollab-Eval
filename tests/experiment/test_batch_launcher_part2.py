"""Offline behavior checks for research batches and cell reports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencollab_eval.commands import batch as batch_cli
from opencollab_eval.experiment import batch_score
from opencollab_eval.experiment.batch_spec import (
    SpecError,
    load_host,
    load_spec,
    spec_digest,
)
from tests.experiment.batch_support import (
    _NEEDS_PROC as _NEEDS_PROC,
)
from tests.experiment.batch_support import (
    EXPERIMENT as EXPERIMENT,
)
from tests.experiment.batch_support import (
    HAND_LAUNCHED_ARGV as HAND_LAUNCHED_ARGV,
)
from tests.experiment.batch_support import (
    HAND_LAUNCHED_INSTANCES_SHA256 as HAND_LAUNCHED_INSTANCES_SHA256,
)
from tests.experiment.batch_support import (
    PIN_EVAL as PIN_EVAL,
)
from tests.experiment.batch_support import (
    PIN_OC as PIN_OC,
)
from tests.experiment.batch_support import (
    REAL_CACHE as REAL_CACHE,
)
from tests.experiment.batch_support import (
    ROOT as ROOT,
)
from tests.experiment.batch_support import (
    FakeRemote as FakeRemote,
)
from tests.experiment.batch_support import (
    ScoreRemote as ScoreRemote,
)
from tests.experiment.batch_support import (
    SyncRemote as SyncRemote,
)
from tests.experiment.batch_support import (
    _agent_file as _agent_file,
)
from tests.experiment.batch_support import (
    _bash as _bash,
)
from tests.experiment.batch_support import (
    _checkout_pythonpath as _checkout_pythonpath,
)
from tests.experiment.batch_support import (
    _checks as _checks,
)
from tests.experiment.batch_support import (
    _facts as _facts,
)
from tests.experiment.batch_support import (
    _git as _git,
)
from tests.experiment.batch_support import (
    _good_facts as _good_facts,
)
from tests.experiment.batch_support import (
    _guard as _guard,
)
from tests.experiment.batch_support import (
    _guard_facts_with_a_driver as _guard_facts_with_a_driver,
)
from tests.experiment.batch_support import (
    _score as _score,
)
from tests.experiment.batch_support import (
    _score_guard as _score_guard,
)
from tests.experiment.batch_support import (
    _spec_for_fake_host as _spec_for_fake_host,
)
from tests.experiment.batch_support import (
    _spec_text as _spec_text,
)
from tests.experiment.batch_support import (
    _sync as _sync,
)
from tests.experiment.batch_support import (
    _synced as _synced,
)
from tests.experiment.batch_support import (
    _with_scoring_dataset as _with_scoring_dataset,
)
from tests.experiment.batch_support import (
    experiment as experiment,
)
from tests.experiment.batch_support import (
    fake_host as fake_host,
)
from tests.experiment.batch_support import (
    oc_repo as oc_repo,
)


def test_plan_after_preflight_keeps_the_host_facts(experiment: dict) -> None:
    remote = FakeRemote(_facts(experiment))
    args = ["--experiment-dir", str(experiment["dir"])]
    assert batch_cli.main([*args, "preflight", str(experiment["spec"])], remote_factory=lambda h: remote) == 0
    assert batch_cli.main([*args, "plan", str(experiment["spec"])], remote_factory=lambda h: None) == 0
    record_path = Path(load_host(experiment["dir"] / "hosts" / "h.yaml").local_batches_dir) / "t1.launch" / "batch.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["host"]["model"] == "deepseek-v4-flash"
    assert (
        batch_cli.main([*args, "launch", str(experiment["spec"]), "--limit", "1"], remote_factory=lambda h: remote) == 0
    )
    assert batch_cli.main([*args, "launch", str(experiment["spec"])], remote_factory=lambda h: remote) == 0
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert [launch["limit"] for launch in record["launches"]] == [1, None]
    assert batch_cli.main([*args, "plan", str(experiment["spec"])], remote_factory=lambda h: None) == 0
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert len(record["launches"]) == 2 and "host" in record


def test_sync_checks_out_both_repositories_at_the_spec_pins(experiment: dict, capsys) -> None:
    remote = SyncRemote(_guard(), _synced(experiment))

    assert _sync(experiment, remote) == 0

    out = capsys.readouterr().out
    assert "RESULT: synced" in out
    checkout = remote.scripts[1]
    assert experiment["sha"] in checkout and PIN_EVAL in checkout
    # Fetch only when the commit is not already there, so a repeat sync is free.
    assert 'cat-file -e "$sha^{commit}"' in checkout


def test_sync_refuses_while_a_driver_is_alive(experiment: dict, capsys) -> None:
    """A checkout would move the code under a batch that is still running.

    Nothing may be written, so the assertion is on the scripts sent: the guard
    and nothing else.
    """
    remote = SyncRemote(_guard(running="4242 python -m ...gen_prediction_batch --out-dir t1"))

    assert _sync(experiment, remote) == 1

    out = capsys.readouterr().out
    assert "REFUSED" in out and "driver(s) alive" in out
    assert len(remote.scripts) == 1
    assert "sync_repo()" not in remote.scripts[0]


def test_sync_refuses_when_its_own_driver_check_finds_no_decoy(experiment: dict, capsys) -> None:
    """A quiet result from a check that matches nothing is not evidence.

    The guard plants a process whose command line contains the pattern. If that
    is not found, "no driver is running" carries no information and the sync
    must not proceed on it.
    """
    remote = SyncRemote(_guard(decoy="0"))

    assert _sync(experiment, remote) == 1

    out = capsys.readouterr().out
    assert "REFUSED" in out and "decoy" in out
    assert len(remote.scripts) == 1


def test_sync_refuses_on_a_modified_tree(experiment: dict, capsys) -> None:
    remote = SyncRemote(_guard(dirty="3"))

    assert _sync(experiment, remote) == 1

    out = capsys.readouterr().out
    assert "REFUSED" in out and "modified tracked files" in out
    assert len(remote.scripts) == 1


def test_sync_reports_a_checkout_that_did_not_land(experiment: dict, capsys) -> None:
    """The host answering with some other sha is a failure, not a success."""
    remote = SyncRemote(_guard(), "OC_AFTER\tdeadbeef\nEV_AFTER\tdeadbeef\n")

    assert _sync(experiment, remote) == 1
    assert "RESULT: NOT synced" in capsys.readouterr().out


@_NEEDS_PROC
def test_sync_guard_counts_a_driver_only_against_its_own_checkout(tmp_path: Path) -> None:
    """A batch running from `lthpc` must not stop a sync of `lthpc-b`, and must stop one of `lthpc`.

    The second checkout exists so a second pin can run while a batch holds the
    first; a guard that counts every driver on the machine refuses that sync
    whenever anything runs anywhere, which is the one situation it exists for.
    """
    from_a = _checkout_pythonpath(tmp_path, "lthpc.yaml")

    own = _guard_facts_with_a_driver(tmp_path, from_a, "lthpc.yaml")
    other = _guard_facts_with_a_driver(tmp_path, from_a, "lthpc-b.yaml")

    assert [f[0] for f in own] == ["RUNNING"]
    assert [f[0] for f in other] == ["ELSEWHERE"]


@_NEEDS_PROC
def test_sync_guard_counts_a_driver_it_cannot_attribute_against_every_checkout(tmp_path: Path) -> None:
    """No PYTHONPATH to read means no evidence the driver is elsewhere: refuse, as before."""
    facts = _guard_facts_with_a_driver(tmp_path, None, "lthpc-b.yaml")

    assert [f[0] for f in facts] == ["RUNNING"]


def test_sync_proceeds_past_drivers_on_other_checkouts(experiment: dict, capsys) -> None:
    guard = _guard() + "ELSEWHERE\t4242 python -m ...gen_prediction_batch --out-dir t1\n"
    remote = SyncRemote(guard, _synced(experiment))

    assert _sync(experiment, remote) == 0

    out = capsys.readouterr().out
    assert "RESULT: synced" in out
    assert "1 driver(s) on other checkouts" in out


def test_go_does_not_launch_when_sync_refuses(experiment: dict, capsys) -> None:
    remote = SyncRemote(_guard(running="4242 python -m ...gen_prediction_batch"))

    assert _sync(experiment, remote, command="go") == 1

    out = capsys.readouterr().out
    assert "RESULT: not launched" in out
    assert all("setsid nohup env" not in s for s in remote.scripts)


def test_a_fetch_retry_names_the_proxy_the_host_resolved(experiment: dict, capsys) -> None:
    """A fetch that cannot reach GitHub must say what route it tried.

    Both checkouts on lthpc carried a repository-local ``http.proxy =`` whose
    value was the empty string. It overrides the global setting, and
    ``config --get`` returns it with a zero exit status, so the sync exported
    no proxy, fetched direct, and reported three retries and a missing commit
    without naming the cause. The retry line now carries the resolved proxy,
    and "none" is one of the answers it can carry.
    """
    remote = SyncRemote(
        _guard(),
        "EV_PROXY\tnone\nEV_FETCH_RETRY\t1\nEV_MISSING\t" + PIN_EVAL + "\n"
        f"OC_PROXY\thttp://127.0.0.1:17890\nOC_AFTER\t{experiment['sha']}\n",
    )

    assert _sync(experiment, remote) == 1

    out = capsys.readouterr().out
    assert "fetch retry 1 (proxy none)" in out


def test_the_sync_script_falls_back_to_the_global_proxy(experiment: dict) -> None:
    """The repository-local value is read first, the global one when it is empty."""
    remote = SyncRemote(_guard(), _synced(experiment))

    assert _sync(experiment, remote) == 0

    checkout = remote.scripts[1]
    assert 'git -C "$d" config --get http.proxy' in checkout
    assert "git config --global --get http.proxy" in checkout
    # The printf format carries a real tab and newline, as every fact line in
    # this script does, so the assertion is on the parts either side of them.
    assert '"%s_PROXY' in checkout and '"$tag" "${P:-none}"' in checkout


def test_score_refuses_when_the_host_names_no_dataset(experiment: dict, capsys) -> None:
    """Without a dataset there is nothing to score against, and no guess is safe."""
    remote = ScoreRemote(_score_guard())

    assert _score(experiment, remote) == 1

    assert "no scoring_dataset" in capsys.readouterr().out
    assert remote.scripts == []


def test_score_refuses_when_its_own_running_check_finds_no_decoy(experiment: dict, capsys) -> None:
    _with_scoring_dataset(experiment)
    remote = ScoreRemote(_score_guard(decoy="0"))

    assert _score(experiment, remote) == 1

    assert "decoy" in capsys.readouterr().out
    assert len(remote.scripts) == 1


def test_score_refuses_while_a_harness_is_already_running(experiment: dict, capsys) -> None:
    _with_scoring_dataset(experiment)
    remote = ScoreRemote(_score_guard(running="991 python -m swebench.harness.run_evaluation --run_id x"))

    assert _score(experiment, remote) == 1

    assert "already running" in capsys.readouterr().out
    assert len(remote.scripts) == 1


def test_score_refuses_a_predictions_file_shorter_than_the_batch(experiment: dict, capsys) -> None:
    """A short predictions file scores without complaint and returns a rate
    over the runs that happened to finish. The count is read first."""
    _with_scoring_dataset(experiment)
    remote = ScoreRemote(_score_guard(rows="1"))

    assert _score(experiment, remote) == 1

    out = capsys.readouterr().out
    assert "1 rows of 2 instances" in out and "RESULT: not scored" in out
    assert len(remote.scripts) == 1


def test_score_refuses_when_the_dataset_has_no_row_for_an_instance(experiment: dict, capsys) -> None:
    _with_scoring_dataset(experiment)
    remote = ScoreRemote(_score_guard(), dataset="DATASET_MISSING\tb__b-2\n")

    assert _score(experiment, remote) == 1

    assert "no row for b__b-2" in capsys.readouterr().out


def test_a_gold_run_that_leaves_a_task_unresolved_makes_the_session_unreadable() -> None:
    ok, detail = batch_score.gold_verdict(
        {
            "total": 51,
            "resolved": 50,
            "resolved_ids": [],
            "unresolved_ids": ["pylint-dev__pylint-4661"],
            "error_ids": [],
            "empty_patch_ids": [],
            "submitted": 51,
            "completed": 51,
        }
    )
    assert not ok
    assert "pylint-dev__pylint-4661" in detail


def test_a_gold_run_that_scored_nothing_is_not_a_pass() -> None:
    """Zero of zero is the shape a missing report has, and it must not read as success."""
    ok, detail = batch_score.gold_verdict(
        {
            "total": 0,
            "resolved": 0,
            "resolved_ids": [],
            "unresolved_ids": [],
            "error_ids": [],
            "empty_patch_ids": [],
            "submitted": 0,
            "completed": 0,
        }
    )
    assert not ok and "scored no instances" in detail


def test_the_gold_predictions_are_the_reference_patches() -> None:
    rows = [{"instance_id": "a__a-1", "patch": "diff --git a b"}]
    assert batch_score.gold_predictions(rows) == [
        {"instance_id": "a__a-1", "model_name_or_path": "gold", "model_patch": "diff --git a b"}
    ]


def test_derived_spec_is_refused_by_launch_and_copies_nothing(experiment: dict, capsys) -> None:
    """A spec over assembled predictions must not be launchable.

    Its out-dir holds a predictions file built from a batch that already ran,
    and every other command treats that directory like a batch's own. Launching
    into it would pay for runs that overwrite the artifact being read.
    """
    path = experiment["dir"] / "batches" / "derived.yaml"
    path.write_text(_spec_text(experiment, "name: t1", "name: t1\nderived: true"), encoding="utf-8")
    remote = FakeRemote(_facts(experiment))
    rc = batch_cli.main(
        ["--experiment-dir", str(experiment["dir"]), "launch", str(path)],
        remote_factory=lambda h: remote,
    )
    assert rc == 1
    assert remote.copied == []
    assert not any("setsid nohup env" in s for s in remote.scripts)
    assert "derived spec" in capsys.readouterr().out


def test_derived_does_not_move_the_digest(experiment: dict) -> None:
    """Adding the field must not change the identity of any batch already launched."""
    plain = load_spec(experiment["spec"])
    path = experiment["dir"] / "batches" / "derived2.yaml"
    path.write_text(_spec_text(experiment, "name: t1", "name: t1\nderived: true"), encoding="utf-8")
    assert spec_digest(load_spec(path)) == spec_digest(plain)


def test_derived_cannot_also_be_a_retry(experiment: dict) -> None:
    path = experiment["dir"] / "batches" / "derived3.yaml"
    path.write_text(_spec_text(experiment, "name: t1", "name: t1\nderived: true\nretry_of: t0"), encoding="utf-8")
    with pytest.raises(SpecError, match="cannot also be a retry"):
        load_spec(path)


def test_derived_must_be_a_boolean(experiment: dict) -> None:
    path = experiment["dir"] / "batches" / "derived4.yaml"
    path.write_text(_spec_text(experiment, "name: t1", 'name: t1\nderived: "yes"'), encoding="utf-8")
    with pytest.raises(SpecError, match="must be true or false"):
        load_spec(path)
