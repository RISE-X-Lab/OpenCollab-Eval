"""Exercise workspace integrity policy against real disposable containers."""

from __future__ import annotations

import argparse
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from e2e.deterministic_swe_driver import (
    OWNER_LABEL,
    SOURCE_PATH,
    _build_image,
    _cleanup_docker,
    _run,
    _synthetic_sources,
)
from e2e.integrity_evidence import require_sanitized_snapshot
from opencollab_eval.engine.workspace_integrity import WorkspaceIntegrityError
from opencollab_eval.generation.gen_prediction_patch import (
    extract_patch_trusted,
    prepare_trusted_patch_baseline,
)
from opencollab_eval.generation.gen_prediction_snapshot import prepare_solver_git_snapshot
from opencollab_eval.patch_diff import patch_paths


def _start(image: str, name: str, run_id: str, command: list[str] | None = None) -> str:
    args = [
        "docker", "run", "-d", "--name", name,
        "--label", f"{OWNER_LABEL}={run_id}", "--network", "none", image,
    ]
    if command:
        args.extend(command)
    container_id = _run(args, timeout=30).stdout.strip()
    if len(container_id) != 64:
        raise RuntimeError("Docker did not return a full container id")
    return container_id


def _exec(container: str, command: str) -> str:
    return _run(
        ["docker", "exec", "-w", "/testbed", container, "sh", "-c", command],
        timeout=30,
    ).stdout


def _safe_task(image: str, base_commit: str, run_id: str, suffix: str) -> dict[str, Any]:
    container = _start(image, f"oc-integrity-safe-{run_id}-{suffix}", run_id)
    _exec(
        container,
        f"python3 -c \"from pathlib import Path; p=Path({SOURCE_PATH!r}); "
        "p.write_text(p.read_text().replace('return left - right', 'return 999'))\"",
    )
    snapshot = prepare_solver_git_snapshot(container, base_commit)
    required = {
        "ignored_content",
        "outward_symlink",
        "repository_history_and_configuration",
        "tracked_content_drift",
        "untracked_content",
    }
    require_sanitized_snapshot(snapshot.as_dict(), required)
    _exec(
        container,
        "test ! -e leaked-answer.txt && test ! -e .cache/result && "
        "test ! -e build/result && test ! -e nested-residue && test -L optional-runtime && "
        "test \"$(git rev-list --all --count)\" = 1 && "
        "! git rev-parse --verify refs/heads/future-answer >/dev/null 2>&1 && "
        f"grep -F 'return left - right' {SOURCE_PATH!r}",
    )
    baseline = prepare_trusted_patch_baseline(container, snapshot)
    try:
        _exec(
            container,
            f"python3 -c \"from pathlib import Path; p=Path({SOURCE_PATH!r}); "
            "p.write_text(p.read_text().replace('return left - right', 'return left + right'))\"",
        )
        patch, proof = extract_patch_trusted(container, baseline)
    finally:
        baseline.cleanup()
    if patch_paths(patch) != [SOURCE_PATH] or proof.patch_bytes <= 0:
        raise RuntimeError("trusted extraction did not preserve the exact candidate")
    return {"scenario": suffix, "status": "passed", "patch_sha256": proof.patch_sha256}


def _blocked_image_task(image: str, run_id: str) -> dict[str, Any]:
    container = _start(image, f"oc-integrity-blocked-{run_id}", run_id)
    base_commit = _exec(
        container,
        "printf 'reference answer\\n' > /reference-answer && rm optional-runtime && "
        "ln -s /reference-answer optional-runtime && git add optional-runtime && "
        "git commit -qm readable-answer-link && git rev-parse HEAD",
    ).strip()
    try:
        prepare_solver_git_snapshot(container, base_commit)
    except WorkspaceIntegrityError as exc:
        report = exc.integrity_report
        kinds = {
            item.get("observed_state", {}).get("kind")
            for item in report.get("findings", [])
            if isinstance(item, dict) and isinstance(item.get("observed_state"), dict)
        }
        if exc.failure_scope.value != "image" or "outward_symlink" not in kinds:
            raise RuntimeError("readable answer link returned the wrong failure scope") from exc
        return {"scenario": "readable_answer_link", "status": "blocked", "failure_scope": "image"}
    raise RuntimeError("readable answer link reached the solver snapshot")


def _background_task(image: str, base_commit: str, run_id: str) -> dict[str, Any]:
    writer = (
        "import os,time; p='" + SOURCE_PATH + "'; "
        "\nwhile True:\n f=open(p,'a'); f.write('# background\\n'); "
        "f.flush(); os.fsync(f.fileno()); f.close(); time.sleep(.001)"
    )
    container = _start(image, f"oc-integrity-background-{run_id}", run_id)
    snapshot = prepare_solver_git_snapshot(container, base_commit)
    baseline = prepare_trusted_patch_baseline(container, snapshot)
    try:
        _run(
            ["docker", "exec", "-d", "-w", "/testbed", container, "python3", "-c", writer],
            timeout=30,
        )
        _exec(
            container,
            "i=0; until grep -q '# background' "
            f"{SOURCE_PATH!r}; do i=$((i+1)); [ \"$i\" -lt 1000 ] || exit 1; "
            "sleep .01; done",
        )
        patch, proof = extract_patch_trusted(container, baseline)
        _exec(
            container,
            f"python3 -c \"import pathlib,time; p=pathlib.Path({SOURCE_PATH!r}); "
            "before=p.read_bytes(); time.sleep(.2); assert p.read_bytes()==before\"",
        )
        if patch_paths(patch) != [SOURCE_PATH] or proof.patch_bytes <= 0:
            raise RuntimeError("quiesced background write did not produce a bound candidate")
        return {
            "scenario": "background_write_quiesced",
            "status": "passed",
            "patch_sha256": proof.patch_sha256,
        }
    finally:
        baseline.cleanup()


def run(output: Path) -> dict[str, Any]:
    run_id = "integrity-" + uuid.uuid4().hex[:12]
    work = output.parent / ("." + output.name + ".work")
    if output.exists() or work.exists():
        raise RuntimeError("integrity smoke output already exists")
    work.mkdir(parents=True)
    image = f"opencollab-integrity-{run_id}:latest"
    started = time.monotonic()
    try:
        context = work / "context"
        _synthetic_sources(context, run_id)
        base_commit = _build_image(context, [image])
        tasks = (
            lambda: _safe_task(image, base_commit, run_id, "safe-a"),
            lambda: _blocked_image_task(image, run_id),
            lambda: _safe_task(image, base_commit, run_id, "safe-b"),
        )
        results = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(task) for task in tasks]
            for future in as_completed(futures):
                results.append(future.result())
        results.append(_background_task(image, base_commit, run_id))
        passed = sum(item["status"] == "passed" for item in results)
        blocked = sum(item["status"] == "blocked" for item in results)
        if passed != 3 or blocked != 1:
            raise RuntimeError("concurrent integrity smoke returned incomplete outcomes")
        report = {
            "schema": "opencollab.integrity_docker_smoke.v1",
            "run_id": run_id,
            "base_commit": base_commit,
            "results": sorted(results, key=lambda item: item["scenario"]),
            "concurrent_local_failure_isolated": True,
            "elapsed_seconds": time.monotonic() - started,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return report
    finally:
        cleanup = _cleanup_docker(run_id, [image])
        if work.exists():
            import shutil

            shutil.rmtree(work)
        if not all(cleanup.values()):
            raise RuntimeError("integrity smoke cleanup verification failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.output.resolve()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
