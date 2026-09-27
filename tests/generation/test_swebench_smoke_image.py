"""Smoke generation selects images from current and legacy task records."""

from opencollab_eval.commands.swebench_smoke_spec import instance_image


def test_swebench5_dataset_image_metadata_is_used():
    assert (
        instance_image(
            {"instance_id": "task-1", "image": "swebench/custom-task:ready"},
            namespace="swebench",
            arch="x86_64",
        )
        == "swebench/custom-task:ready"
    )


def test_legacy_record_keeps_the_published_image_name():
    assert (
        instance_image(
            {"instance_id": "OWNER__REPO-1"},
            namespace="swebench",
            arch="arm64",
        )
        == "swebench/sweb.eval.arm64.owner_1776_repo-1:latest"
    )
