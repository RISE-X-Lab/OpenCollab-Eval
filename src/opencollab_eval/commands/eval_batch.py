"""JSONL batch entrypoint for the migrated evaluation engine."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from opencollab_eval.engine import evaluator
from opencollab_eval.engine.evaluator import EvalTask
from opencollab_eval.engine.swe_eval_records import open_regular_binary

MAX_EVAL_TASK_FILE_BYTES = 64 * 1024 * 1024
MAX_EVAL_TASK_LINE_BYTES = 8 * 1024 * 1024
MAX_EVAL_TASKS = 10_000


def _result_counts(results: list[Any]) -> tuple[int, int]:
    eligible_patches = sum(
        1 for result in results if result.patch_produced and result.submission_eligible
    )
    ineligible_results = sum(1 for result in results if not result.submission_eligible)
    return eligible_patches, ineligible_results


def _read_task_payloads(tasks_file: str) -> list[tuple[int, dict[str, Any]]]:
    path = Path(tasks_file)
    payloads: list[tuple[int, dict[str, Any]]] = []
    try:
        with open_regular_binary(path) as handle:
            file_size = os.fstat(handle.fileno()).st_size
            if file_size > MAX_EVAL_TASK_FILE_BYTES:
                raise ValueError(
                    f"eval tasks file exceeds {MAX_EVAL_TASK_FILE_BYTES}-byte limit: {tasks_file}"
                )
            bytes_read = 0
            line_number = 0
            while True:
                remaining_file_bytes = MAX_EVAL_TASK_FILE_BYTES - bytes_read
                line = handle.readline(min(MAX_EVAL_TASK_LINE_BYTES, remaining_file_bytes) + 1)
                if not line:
                    break
                line_number += 1
                bytes_read += len(line)
                if bytes_read > MAX_EVAL_TASK_FILE_BYTES:
                    raise ValueError(
                        f"eval tasks file exceeds {MAX_EVAL_TASK_FILE_BYTES}-byte limit: {tasks_file}"
                    )
                if len(line) > MAX_EVAL_TASK_LINE_BYTES:
                    raise ValueError(
                        f"eval task line {line_number} exceeds {MAX_EVAL_TASK_LINE_BYTES}-byte limit"
                    )
                if not line.strip():
                    continue
                try:
                    data = json.loads(line.decode("utf-8"))
                except UnicodeDecodeError as exc:
                    raise ValueError(f"eval task line {line_number} is not valid UTF-8") from exc
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"eval task line {line_number} is not valid JSON: {exc.msg}"
                    ) from exc
                except (ValueError, RecursionError) as exc:
                    raise ValueError(f"eval task line {line_number} cannot be decoded safely") from exc
                if not isinstance(data, dict):
                    raise ValueError(f"eval task line {line_number} must be a JSON object")
                payloads.append((line_number, data))
                if len(payloads) > MAX_EVAL_TASKS:
                    raise ValueError(f"eval tasks file exceeds {MAX_EVAL_TASKS}-task limit")
    except OSError as exc:
        raise ValueError(f"eval tasks file must be a readable regular file: {tasks_file}") from exc
    return payloads


async def _eval(
    tasks_file: str,
    model: str,
    provider: str,
    api_key: str | None,
    base_url: str | None,
    output_dir: str,
    concurrency: int,
    max_tokens: int,
    timeout: float,
    temperature: float,
    top_p: float | None = None,
    thinking: bool = False,
    thinking_params: dict[str, Any] | None = None,
) -> list[Any]:
    tasks: list[EvalTask] = []
    for line_number, data in _read_task_payloads(tasks_file):
        extras = data.get("extras")
        if extras is not None and not isinstance(extras, dict):
            raise ValueError(f"eval task line {line_number} extras must be a JSON object")
        if isinstance(extras, dict) and "test_patch" in extras and not isinstance(extras["test_patch"], str):
            raise ValueError(f"eval task line {line_number} extras test_patch must be a string")
        try:
            task_id = data["task_id"]
            description = data["description"]
        except KeyError as exc:
            raise ValueError(f"eval task line {line_number} is missing {exc.args[0]!r}") from exc
        tasks.append(
            EvalTask(
                task_id=task_id,
                description=description,
                repo_path=data.get("repo_path"),
                docker_image=data.get("docker_image"),
                timeout=data.get("timeout", timeout),
                max_tokens=data.get("max_tokens", max_tokens),
                extras=extras,
                harness_artifact_paths=(os.path.abspath(tasks_file),),
            )
        )

    results = await evaluator.run_eval_batch(
        tasks,
        concurrency=concurrency,
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        output_dir=output_dir,
        temperature=temperature,
        top_p=top_p,
        thinking=thinking,
        thinking_params=thinking_params,
    )
    evaluator.save_results(results, os.path.join(output_dir, "results.jsonl"))
    return results


__all__ = ["_eval", "_read_task_payloads", "_result_counts"]
