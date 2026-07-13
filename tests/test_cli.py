from __future__ import annotations

import json

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

    assert main(["inspect", str(dataset)]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["count"] == 1
    assert payload["public_task_ids"][0].startswith("solver-")
    assert "secret" not in output

