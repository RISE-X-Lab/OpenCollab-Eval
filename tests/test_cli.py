from __future__ import annotations

import json
from types import SimpleNamespace

from opencollab_eval import cli
from opencollab_eval.cli import main


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
