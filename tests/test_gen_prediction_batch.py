"""What an unattended batch has to get right before it is left alone.

Each test here pins one way a batch of twenty runs comes back unusable without
anything having crashed: the pairs it produced are not pairs, work already paid
for is paid for twice, or the machine it ran on had no image under the name the
generator derived.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencollab_eval.generation import gen_prediction_batch as batch


def _instance(iid: str, image: str | None = None) -> dict[str, object]:
    record: dict[str, object] = {"instance_id": iid, "repo": "acme/widget"}
    if image is not None:
        record["image"] = image
    return record


def test_arms_are_interleaved_so_a_stopped_batch_still_has_pairs() -> None:
    instances = [_instance("a-1"), _instance("a-2"), _instance("a-3")]

    plan = batch.plan_batch(instances, ["single", "team"], {})

    # Task-major: both arms of a-1 come before either arm of a-2. Stopping
    # after any even prefix leaves whole pairs, which is the unit the paired
    # comparison is made of.
    assert [(inst["instance_id"], arm) for inst, arm in plan] == [
        ("a-1", "single"),
        ("a-1", "team"),
        ("a-2", "single"),
        ("a-2", "team"),
        ("a-3", "single"),
        ("a-3", "team"),
    ]


def test_work_already_in_the_predictions_file_is_not_run_again() -> None:
    instances = [_instance("a-1"), _instance("a-2")]
    done = {"single": {"a-1"}, "team": set()}

    plan = batch.plan_batch(instances, ["single", "team"], done)

    assert [(inst["instance_id"], arm) for inst, arm in plan] == [
        ("a-1", "team"),
        ("a-2", "single"),
        ("a-2", "team"),
    ]


def test_completed_ids_come_from_the_predictions_file(tmp_path: Path) -> None:
    predictions = tmp_path / "preds-single.jsonl"
    predictions.write_text(
        json.dumps({"instance_id": "a-1", "model_patch": ""})
        + "\n"
        + "this line is not json\n"
        + json.dumps({"instance_id": "a-2", "model_patch": "diff"})
        + "\n",
        encoding="utf-8",
    )

    # A line that cannot be read is not evidence that its run happened, and it
    # is not a reason to refuse to continue the batch either.
    assert batch.completed_instance_ids(predictions) == {"a-1", "a-2"}
    assert batch.completed_instance_ids(tmp_path / "absent.jsonl") == set()


def test_the_instance_s_own_image_name_is_the_one_passed_down(tmp_path: Path) -> None:
    command = batch.build_command(
        arm="single",
        instance_path=tmp_path / "a-1.json",
        predictions=tmp_path / "preds-single.jsonl",
        team_config=None,
        budget_per_seat=1000,
        max_steps=5,
        timeout=60.0,
        image="swebench/sweb.eval.x86_64.acme_1776_widget-1:latest",
    )

    # Without this the generator derives `sweb.eval.<arch>.<id>:latest`, which
    # is not the name a host that pulled the published images actually has.
    assert "--image" in command
    assert command[command.index("--image") + 1].startswith("swebench/")


def test_only_the_team_arm_is_handed_a_team_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(batch, "declared_role_names", lambda path: ("a", "b", "c"))
    shared = dict(
        instance_path=tmp_path / "a-1.json",
        predictions=tmp_path / "preds.jsonl",
        team_config=tmp_path / "team.yaml",
        budget_per_seat=1000,
        max_steps=5,
        timeout=60.0,
        image=None,
    )

    single = batch.build_command(arm="single", **shared)
    team = batch.build_command(arm="team", **shared)

    assert "--team-config" not in single
    assert "--team-config" in team
    assert single[2] == batch.ARM_MODULES["single"]
    assert team[2] == batch.ARM_MODULES["team"]


def test_the_team_arm_refuses_to_run_without_its_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="team-config"):
        batch.build_command(
            arm="team",
            instance_path=tmp_path / "a-1.json",
            predictions=tmp_path / "preds.jsonl",
            team_config=None,
            budget_per_seat=1000,
            max_steps=5,
            timeout=60.0,
            image=None,
        )


def test_a_seat_is_worth_the_same_on_both_arms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Equal pools would not be equal budgets.

    A team divides its pool by the number of roles its file declares, so
    handing a three-role team the pool a solo agent gets leaves each seat with
    a third of it. What has to match across arms is the seat, so the driver
    multiplies -- and the step and time ceilings, which are per session on both
    arms, are passed through unchanged.
    """
    monkeypatch.setattr(
        batch, "declared_role_names", lambda path: ("analyst", "coder", "tester")
    )
    shared = dict(
        instance_path=tmp_path / "a-1.json",
        predictions=tmp_path / "preds.jsonl",
        team_config=tmp_path / "team.yaml",
        budget_per_seat=1_000_000,
        max_steps=60,
        timeout=1800.0,
        image=None,
    )

    def value(command: list[str], flag: str) -> str:
        return command[command.index(flag) + 1]

    single = batch.build_command(arm="single", **shared)
    team = batch.build_command(arm="team", **shared)

    assert value(single, "--budget") == "1000000"
    assert value(team, "--budget") == "3000000"
    for flag in ("--max-steps", "--timeout"):
        assert value(single, flag) == value(team, flag), flag


def test_the_pool_follows_the_team_file_rather_than_a_remembered_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The count comes out of the file, so adding a fourth role changes the pool
    # without anyone having to notice.
    monkeypatch.setattr(batch, "declared_role_names", lambda path: ("a", "b", "c", "d"))

    assert batch.pool_for("single", 500, None) == 500
    assert batch.pool_for("team", 500, tmp_path / "four.yaml") == 2000


def test_a_directory_of_instances_is_read_in_a_fixed_order(tmp_path: Path) -> None:
    for iid in ("c-3", "a-1", "b-2"):
        (tmp_path / f"{iid}.json").write_text(json.dumps(_instance(iid)), encoding="utf-8")

    loaded = batch.load_instances(tmp_path)

    # A resumed batch has to continue in the order the first one used.
    assert [record["instance_id"] for record in loaded] == ["a-1", "b-2", "c-3"]


def test_a_jsonl_list_of_instances_keeps_its_file_order(tmp_path: Path) -> None:
    source = tmp_path / "instances.jsonl"
    source.write_text(
        "\n".join(json.dumps(_instance(iid)) for iid in ("c-3", "a-1")) + "\n",
        encoding="utf-8",
    )

    assert [r["instance_id"] for r in batch.load_instances(source)] == ["c-3", "a-1"]


def test_a_file_that_is_not_an_instance_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "notes.json"
    path.write_text(json.dumps({"note": "no instance_id here"}), encoding="utf-8")

    with pytest.raises(ValueError, match="not an instance record"):
        batch.load_instances(path)
