from __future__ import annotations

import json
from types import SimpleNamespace

from opencollab_eval import cli
from opencollab_eval.cli import main
from opencollab_eval.commands import run_swebench_eval_per_instance


def test_inspect_reports_only_public_task_ids(tmp_path, capsys) -> None:
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "instance_id": "owner__repo-secret-commit",
                "repo": "owner/repo",
                "problem_statement": "Fix it.",
                "base_commit": "secret-base",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    identity_key = tmp_path / "identity.key"
    identity_key.write_bytes(b"k" * 32)

    assert main(["inspect", str(dataset), "--identity-key-file", str(identity_key)]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["count"] == 1
    assert payload["public_task_ids"][0].startswith("solver-")
    assert "secret" not in output

    assert main(["inspect", str(dataset), "--identity-key-file", str(identity_key)]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["public_task_ids"] == payload["public_task_ids"]


def test_inspect_accepts_explicit_image_repository_for_tag_only_rows(
    tmp_path,
    capsys,
) -> None:
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "instance_id": "owner__repo-1",
                "repo": "owner/repo",
                "problem_statement": "Fix it.",
                "dockerhub_tag": "task-image",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    identity_key = tmp_path / "identity.key"
    identity_key.write_bytes(b"k" * 32)

    assert (
        main(
            [
                "inspect",
                str(dataset),
                "--identity-key-file",
                str(identity_key),
                "--image-repository",
                "registry.example.invalid/swebench",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["count"] == 1


def test_run_delegates_to_migrated_evaluator(monkeypatch, tmp_path, capsys) -> None:
    captured = {}

    async def fake_eval(**kwargs):
        captured.update(kwargs)
        return [
            SimpleNamespace(patch_produced=True, submission_eligible=True),
            SimpleNamespace(patch_produced=False, submission_eligible=False),
        ]

    monkeypatch.setattr(cli, "_eval", fake_eval)
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text("", encoding="utf-8")
    output = tmp_path / "output"

    assert main(
        [
            "run",
            str(tasks),
            "--model",
            "model",
            "--provider",
            "provider",
            "--output",
            str(output),
        ]
    ) == 0
    assert captured["tasks_file"] == str(tasks)
    assert captured["output_dir"] == str(output)
    assert json.loads(capsys.readouterr().out) == {
        "tasks": 2,
        "eligible_patches": 1,
        "ineligible": 1,
    }


def test_score_delegates_to_official_harness_wrapper(monkeypatch) -> None:
    captured = {}

    def fake_runner(argv):
        captured["argv"] = list(argv)
        return 17

    monkeypatch.setattr(run_swebench_eval_per_instance, "main", fake_runner)

    assert main(["score", "--dataset", "dataset.jsonl"]) == 17
    assert captured == {"argv": ["--dataset", "dataset.jsonl"]}


def test_run_loads_caller_workflow(monkeypatch, tmp_path) -> None:
    module = tmp_path / "caller_workflow.py"
    module.write_text("async def solve(context, args):\n    return args\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    captured = {}

    async def fake_eval(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(cli, "_eval", fake_eval)
    assert main([
        "run", str(tmp_path / "tasks.jsonl"), "--model", "model", "--provider", "openai",
        "--workflow", "caller_workflow:solve",
    ]) == 0
    assert captured["workflow"].__module__ == "caller_workflow"
    assert captured["workflow"].__name__ == "solve"
