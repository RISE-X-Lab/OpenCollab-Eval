"""Run the deterministic SWE scenario around installed production commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from e2e.evidence_publish import publish_production_evidence
from e2e.fake_openai_server import (
    EXPECTED_THINKING,
    FAKE_API_KEY,
    MODEL,
    PROVIDER_KEY_NAMES,
    SOURCE_PATH,
)
from e2e.integrity_evidence import require_sanitized_snapshot
from opencollab_eval.engine.swe_generation_proof import current_generation_proof_valid
from opencollab_eval.patch_diff import patch_paths

TARGET_TEST = "test_calculator.py::test_add"
OWNER_LABEL = "opencollab.eval.deterministic-e2e"
CANARY = "opencollab-real-key-canary-must-not-propagate"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object: {path}")
    return value


def _jsonl_one(path: Path) -> dict[str, Any]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise RuntimeError(f"expected exactly one JSONL object: {path}")
    return rows[0]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _clean_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    allowed = {
        key: os.environ[key]
        for key in ("HOME", "LANG", "LC_ALL", "PATH", "SHELL", "TMPDIR")
        if os.environ.get(key)
    }
    allowed.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    allowed.setdefault("HOME", str(Path.home()))
    allowed.setdefault("LANG", "C.UTF-8")
    if extra:
        allowed.update(extra)
    if any(name in allowed for name in PROVIDER_KEY_NAMES):
        raise RuntimeError("provider credentials entered the deterministic child environment")
    return allowed


def _terminate_process_group(process: subprocess.Popen[str], *, grace: float = 15) -> bool:
    if process.poll() is not None:
        return True
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    try:
        process.wait(timeout=grace)
        return True
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        try:
            process.wait(timeout=5)
            return True
        except subprocess.TimeoutExpired:
            return False


def _run(
    command: list[str],
    *,
    timeout: float = 120,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env or _clean_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        _terminate_process_group(process)
        raise
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and result.returncode != 0:
        detail = (stderr or stdout).strip()
        raise RuntimeError(f"{shlex.join(command)} failed ({result.returncode}): {detail[-6000:]}")
    return result


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _docker_architecture() -> str:
    result = _run(["docker", "info", "--format", "{{.Architecture}}"], timeout=30)
    value = result.stdout.strip().lower()
    if value in {"amd64", "x86_64"}:
        return "x86_64"
    if value in {"arm64", "aarch64"}:
        return "arm64"
    raise RuntimeError(f"unsupported Docker architecture: {value}")


def _synthetic_sources(context: Path, run_id: str) -> None:
    source = context / SOURCE_PATH
    source.parent.mkdir(parents=True)
    source.write_text(
        '"""Small arithmetic library used by the deterministic evaluation."""\n\n\n'
        "def add(left: int, right: int) -> int:\n"
        "    \"\"\"Return the sum of two integers.\"\"\"\n"
        "    return left - right\n",
        encoding="utf-8",
    )
    (context / "Dockerfile").write_text(
        "FROM python:3.11-slim\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends git && "
        "rm -rf /var/lib/apt/lists/* && python -m pip install --no-cache-dir pytest==8.3.5\n"
        "RUN mkdir -p /opt/miniconda3/bin && printf '%s\\n' "
        "'conda() { export CONDA_DEFAULT_ENV=\"${2:-${1:-testbed}}\"; return 0; }' "
        "'export -f conda' > /opt/miniconda3/bin/activate\n"
        "WORKDIR /testbed\n"
        f'COPY ["{SOURCE_PATH}", "/testbed/{SOURCE_PATH}"]\n'
        "RUN printf '.cache/\\nbuild/\\n' > .gitignore && "
        "printf '#!/bin/sh\\nexit 0\\n' > baseline-tool && chmod 755 baseline-tool && "
        "ln -s ../../missing-runtime-target optional-runtime\n"
        "RUN git init -b main && git config user.email e2e@opencollab.local && "
        "git config user.name 'OpenCollab E2E' && git add . && "
        "GIT_AUTHOR_DATE='2024-01-01T00:00:00Z' GIT_COMMITTER_DATE='2024-01-01T00:00:00Z' "
        "git commit -m baseline && base=$(git rev-parse HEAD) && "
        "printf 'reference answer\\n' > leaked-answer.txt && git add leaked-answer.txt && "
        "git commit -m future-answer && git branch future-answer && git reset --hard $base && "
        "printf 'reference answer\\n' > leaked-answer.txt && mkdir -p .cache build && "
        "printf 'cache residue\\n' > .cache/result && printf 'build residue\\n' > build/result && "
        "git init -q nested-residue\n"
        f'LABEL {OWNER_LABEL}="{run_id}"\n'
        "CMD [\"tail\", \"-f\", \"/dev/null\"]\n",
        encoding="utf-8",
    )


def _test_patch() -> str:
    return (
        "diff --git a/test_calculator.py b/test_calculator.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/test_calculator.py\n"
        "@@ -0,0 +1,11 @@\n"
        "+import importlib.util\n"
        "+from pathlib import Path\n"
        "+\n"
        "+\n"
        "+def test_add():\n"
        f"+    source = Path({SOURCE_PATH!r})\n"
        "+    spec = importlib.util.spec_from_file_location('deterministic_calculator', source)\n"
        "+    module = importlib.util.module_from_spec(spec)\n"
        "+    assert spec.loader is not None\n"
        "+    spec.loader.exec_module(module)\n"
        "+    assert module.add(2, 3) == 5\n"
    )


def _build_image(context: Path, tags: list[str]) -> str:
    _run(["docker", "build", "--pull=false", "--tag", tags[0], str(context)], timeout=300)
    for tag in tags[1:]:
        _run(["docker", "tag", tags[0], tag], timeout=30)
    result = _run(["docker", "run", "--rm", "--network", "none", tags[0], "git", "rev-parse", "HEAD"])
    commit = result.stdout.strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise RuntimeError("synthetic image returned an invalid base commit")
    return commit


def _baseline_probe(image: str, patch: str, run_id: str) -> dict[str, Any]:
    name = f"oc-e2e-baseline-{run_id}"
    command = [
        "docker", "run", "--rm", "-i", "--name", name,
        "--label", f"{OWNER_LABEL}={run_id}", "--network", "none", image,
        "bash", "-lc", "git apply - && PYTHONDONTWRITEBYTECODE=1 pytest -q test_calculator.py::test_add",
    ]
    result = subprocess.run(
        command,
        input=patch,
        text=True,
        capture_output=True,
        timeout=60,
        env=_clean_environment(),
        check=False,
    )
    output = result.stdout + result.stderr
    if result.returncode == 0 or TARGET_TEST not in output:
        raise RuntimeError(
            "synthetic baseline did not fail the target test: "
            f"returncode={result.returncode}; output={output[-2000:]}"
        )
    return {
        "executed": True,
        "returncode": result.returncode,
        "target": TARGET_TEST,
        "target_failed": True,
        "output_sha256": _sha256_bytes(output.encode()),
    }


def wait_for_service(process: subprocess.Popen[str], ready: Path, *, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("fake model service exited before becoming ready")
        if ready.is_file():
            return
        time.sleep(0.05)
    raise RuntimeError("fake model service did not become ready")


def _start_fake_service(
    artifact_dir: Path, *, forbidden_env_value: str = CANARY
) -> tuple[subprocess.Popen[str], str]:
    port = _free_port()
    ready = artifact_dir / "fake-model.ready"
    trace = artifact_dir / "fake-model-trace.jsonl"
    log = (artifact_dir / "fake-model.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m", "e2e.fake_openai_server",
            "--port", str(port), "--trace", str(trace), "--ready-file", str(ready),
            "--forbidden-env-value", forbidden_env_value,
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
        env=_clean_environment(),
        cwd=Path(__file__).parents[1],
    )
    (artifact_dir / "fake-model.pid").write_text(str(process.pid) + "\n", encoding="ascii")
    try:
        wait_for_service(process, ready)
    except BaseException:
        _terminate_process_group(process)
        log.close()
        raise
    log.close()
    return process, f"http://127.0.0.1:{port}/v1"


def _stop_fake_service(process: subprocess.Popen[str]) -> bool:
    return _terminate_process_group(process, grace=5)


def _write_dataset(remote_root: Path, instance: dict[str, Any]) -> None:
    dataset_dir = remote_root / "datasets" / "swe-batch-pro-lite"
    dataset_dir.mkdir(parents=True)
    payload = json.dumps(instance, sort_keys=True) + "\n"
    (dataset_dir / "instances.jsonl").write_text(payload, encoding="utf-8")


def _production_command(
    *,
    executable: Path,
    ssh_command: str,
    host: str,
    remote_root: Path,
    runtime: Path,
    run_dir: Path,
    run_id: str,
    image_repository: str,
    local_base_url: str,
    remote_base_url: str,
    proxy_env: Path,
    json_output: Path,
    markdown_output: Path,
) -> list[str]:
    thinking = json.dumps({"thinking": EXPECTED_THINKING}, separators=(",", ":"))
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("docker executable is unavailable")
    return [
        str(executable), "swe-v1-prolite", "--host", host, "--ssh-command", ssh_command,
        "--remote-python", str(executable.parent / "python"),
        "--remote-path-entry", str(Path(docker).parent),
        "--remote-root", str(remote_root), "--remote-runtime-repo", str(runtime),
        "--base-run-dir", str(run_dir), "--run-id", run_id, "--start-index", "1", "--limit", "1",
        "--workflow", "single-agent", "--model-name", MODEL, "--llm-model", MODEL,
        "--llm-provider", "openai", "--context-window", "262144", "--temperature", "1",
        "--top-p", "0.95", "--max-output-tokens", "32768", "--session-prefix", "oc-e2e",
        "--image-repository", image_repository, "--local-proxy-base-url", local_base_url,
        "--remote-proxy-base-url", remote_base_url, "--proxy-env-file", str(proxy_env),
        "--workflow-env", "OPENCOLLAB_THINKING=true", "--workflow-env", f"OPENCOLLAB_THINKING_PARAMS={thinking}",
        "--budget", "5000", "--max-steps", "8", "--swe-timeout", "120",
        "--task-wall-timeout", "150", "--eval-timeout", "120", "--llm-timeout", "60",
        "--max-task-starts", "1", "--max-eval-attempts", "1", "--max-empty-patch-retries", "0",
        "--total-timeout", "300", "--json-output", str(json_output),
        "--markdown-output", str(markdown_output),
    ]


def validate_patch_identity(
    prediction: dict[str, Any], metric: dict[str, Any], *, instance_id: str
) -> tuple[str, str]:
    patch = prediction.get("model_patch")
    if not isinstance(patch, str) or not patch.strip():
        raise RuntimeError("generation produced an empty candidate patch")
    patch_sha = _sha256_bytes(patch.encode("utf-8", errors="surrogatepass"))
    for field, expected in {
        "instance_id": instance_id,
        "model_name_or_path": MODEL,
        "patch_sha256": patch_sha,
    }.items():
        if prediction.get(field) != expected or metric.get(field) != expected:
            raise RuntimeError(f"candidate {field} identity mismatch")
    if prediction.get("record_id") != metric.get("record_id") or not prediction.get("record_id"):
        raise RuntimeError("candidate record identity mismatch")
    if prediction.get("workflow_metric") != metric:
        raise RuntimeError("embedded candidate metric differs from the metrics record")
    return patch, patch_sha


def validate_runtime_identity(metric: dict[str, Any]) -> None:
    for field, expected in {
        "llm_model": MODEL,
        "llm_provider": "openai",
        "context_window": 262144,
        "temperature": 1.0,
        "top_p": 0.95,
        "max_output_tokens": 32768,
    }.items():
        if metric.get(field) != expected:
            raise RuntimeError(f"candidate runtime identity mismatch: {field}")


def _validate_candidate(
    prediction: dict[str, Any],
    metric: dict[str, Any],
    report: dict[str, Any],
    *,
    instance_id: str,
    run_id: str,
) -> dict[str, Any]:
    patch, patch_sha = validate_patch_identity(prediction, metric, instance_id=instance_id)
    validate_runtime_identity(metric)
    if not current_generation_proof_valid(metric, patch):
        raise RuntimeError("candidate lacks a current trusted patch extraction proof")
    snapshot = metric.get("solver_git_snapshot")
    required = {"ignored_content", "outward_symlink", "repository_history_and_configuration", "untracked_content"}
    require_sanitized_snapshot(snapshot, required)
    paths = patch_paths(patch)
    if paths != [SOURCE_PATH]:
        raise RuntimeError(f"candidate patch paths differ from the exact synthetic source: {paths!r}")
    if any(marker in patch for marker in ("__pycache__", ".pyc", "GIT binary patch")):
        raise RuntimeError("candidate patch contains generated or binary artifacts")
    runtime = report.get("runtime_sync", {}).get("source_tree", {})
    identities = [runtime.get(name) for name in ("local", "remote", "pre_generation_remote")]
    if not identities[0] or identities[0] != identities[1] or identities[0] != identities[2]:
        raise RuntimeError("runtime tree was not identically verified before generation")
    for field, expected in {
        "run_id": run_id,
        "workflow": "single-agent",
        "invocation_id": report.get("invocation_id"),
        "runtime_tree_sha256": identities[0]["sha256"],
    }.items():
        if not expected or metric.get(field) != expected:
            raise RuntimeError(f"candidate production invocation identity mismatch: {field}")
    return {
        "schema": "opencollab.deterministic_candidate_proof.v2",
        "instance_id": instance_id,
        "record_id": prediction["record_id"],
        "patch_sha256": patch_sha,
        "patch_paths": paths,
        "run_id": metric["run_id"],
        "workflow": metric["workflow"],
        "invocation_id": metric["invocation_id"],
        "runtime_tree_sha256": metric["runtime_tree_sha256"],
        "trusted_patch_extraction": metric["trusted_patch_extraction"],
    }


@contextmanager
def _working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    path.mkdir(parents=True)
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def validate_official_execution(
    status_map: dict[str, str], output: str, *, found: bool
) -> int:
    match = re.search(r"\b(\d+)\s+passed\b", output)
    collected = int(match.group(1)) if match else 0
    if not found or status_map.get(TARGET_TEST) != "PASSED" or collected != 1:
        raise RuntimeError("official target-test execution proof is incomplete")
    return collected


def _official_eval(
    instance: dict[str, Any],
    prediction: dict[str, Any],
    *,
    architecture: str,
    namespace: str,
    run_id: str,
    workspace: Path,
    artifact_dir: Path,
) -> dict[str, Any]:
    import docker
    from swebench.harness.grading import get_logs_eval
    from swebench.harness.run_evaluation import run_instance
    from swebench.harness.test_spec.test_spec import make_test_spec

    spec = make_test_spec(instance, namespace=namespace, arch=architecture)
    expected_image = f"{namespace}/sweb.eval.{spec.arch}.{instance['instance_id']}:latest"
    if spec.instance_image_key != expected_image:
        raise RuntimeError("official TestSpec selected an unexpected image identity")
    model = str(prediction["model_name_or_path"]).replace("/", "__")
    report_relative = Path("logs") / "run_evaluation" / run_id / model / instance["instance_id"]
    with _working_directory(workspace):
        result = run_instance(
            spec, prediction, False, False, docker.from_env(), run_id,
            timeout=120, rewrite_reports=False,
        )
    report_dir = workspace / report_relative
    report_path = report_dir / "report.json"
    test_output_path = report_dir / "test_output.txt"
    patch_path = report_dir / "patch.diff"
    report = _json(report_path)
    target_report = report.get(instance["instance_id"], {})
    published_report = artifact_dir / "official-report.json"
    published_output = artifact_dir / "official-test-output.txt"
    published_patch = artifact_dir / "official-candidate.patch"
    shutil.copy2(report_path, published_report)
    shutil.copy2(test_output_path, published_output)
    shutil.copy2(patch_path, published_patch)
    status_map, found = get_logs_eval(spec, str(test_output_path))
    output = test_output_path.read_text(encoding="utf-8")
    collected = validate_official_execution(status_map, output, found=found)
    patch_sha = _sha256_file(patch_path)
    expected_sha = prediction["patch_sha256"]
    if result != {"completed": True, "resolved": True} or target_report.get("resolved") is not True:
        raise RuntimeError("official evaluation did not resolve the exact candidate")
    if patch_sha != expected_sha:
        raise RuntimeError("official evaluation patch differs from the bound candidate")
    proof = {
        "schema": "opencollab.deterministic_official_eval.v2",
        "module": "swebench.harness.run_evaluation",
        "function": "run_instance",
        "instance_id": instance["instance_id"],
        "run_id": run_id,
        "container_name": spec.get_instance_container_name(run_id),
        "image": spec.instance_image_key,
        "target_test": TARGET_TEST,
        "test_command": "pytest -rA",
        "collected_tests": collected,
        "target_passed": True,
        "resolved": True,
        "patch_sha256": expected_sha,
        "report_sha256": _sha256_file(published_report),
        "test_output_sha256": _sha256_file(published_output),
        "report_path": str(published_report),
        "test_output_path": str(published_output),
    }
    _write_json(artifact_dir / "official-eval-proof.json", proof)
    return proof


def _validate_terminal_report(
    report: dict[str, Any], markdown: str, official: dict[str, Any], *, instance_id: str
) -> None:
    counts = report.get("counts", {})
    if report.get("status") != "done" or counts.get("tasks") != 1:
        raise RuntimeError("production report is not terminal for exactly one task")
    if counts.get("resolved") != 1 or counts.get("unresolved") != 0 or counts.get("technical_failed") != 0:
        raise RuntimeError("production report counts do not prove one resolved task")
    rows = report.get("rows")
    if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("task") != instance_id:
        raise RuntimeError("production report task census is invalid")
    row = rows[0]
    generation = row.get("generation", {})
    evaluation = row.get("eval", {})
    if generation.get("status") != "generation_done" or evaluation.get("status") != "eval_done":
        raise RuntimeError("production generation or evaluation did not finish")
    if evaluation.get("summary", {}).get("resolved") is not True or official.get("resolved") is not True:
        raise RuntimeError("production and official resolved verdicts differ")
    if generation.get("patch_sha256") != official.get("patch_sha256"):
        raise RuntimeError("production and official patch identities differ")
    if instance_id not in markdown or "resolved" not in markdown.lower():
        raise RuntimeError("production Markdown report differs from the JSON task result")


def _trace_proves_clean_environment(path: Path) -> bool:
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    started = [event for event in events if event.get("event") == "started"]
    generations = [event for event in events if event.get("event") == "generation"]
    raw = path.read_text(encoding="utf-8")
    return (
        len(started) == 1
        and started[0].get("provider_environment_clean") is True
        and len(generations) == 4
        and CANARY not in raw
    )


def _cleanup_docker(run_id: str, images: list[str]) -> dict[str, bool]:
    listed = _run(
        ["docker", "ps", "-aq", "--filter", f"label={OWNER_LABEL}={run_id}"],
        check=False,
        timeout=30,
    )
    container_ids = listed.stdout.split()
    if container_ids:
        _run(["docker", "rm", "-f", *container_ids], check=False, timeout=60)
    image_removed = True
    for image in images:
        result = _run(["docker", "image", "rm", "-f", image], check=False, timeout=60)
        image_removed = image_removed and result.returncode == 0
    remaining = _run(
        ["docker", "ps", "-aq", "--filter", f"label={OWNER_LABEL}={run_id}"],
        check=False,
        timeout=30,
    ).stdout.split()
    return {"owned_containers_removed": not remaining, "owned_images_removed": image_removed}


def run_scenario(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    run_id = args.run_id or f"det-e2e-{uuid.uuid4().hex[:12]}"
    if re.fullmatch(r"[a-z0-9-]+", run_id) is None:
        raise ValueError("run-id must contain lowercase ASCII letters, digits, and hyphens")
    artifact_dir = args.output.resolve() / run_id
    if artifact_dir.exists():
        raise RuntimeError(f"run output already exists: {artifact_dir}")
    artifact_dir.mkdir(parents=True)
    work = artifact_dir / "work"
    work.mkdir()
    fake_process: subprocess.Popen[str] | None = None
    images: list[str] = []
    cleanup: dict[str, Any] = {}
    ledger: list[dict[str, Any]] = []
    try:
        architecture = _docker_architecture()
        instance_id = f"deterministic-calculator-{run_id}"
        image_repository = f"opencollab-e2e-{run_id}/calculator"
        production_image = f"{image_repository}:{instance_id}"
        namespace = f"opencollab-e2e-{run_id}"
        official_image = f"{namespace}/sweb.eval.{architecture}.{instance_id}:latest"
        images = [production_image, official_image]
        context = work / "image-context"
        phase = time.monotonic()
        _synthetic_sources(context, run_id)
        base_commit = _build_image(context, images)
        image_seconds = time.monotonic() - phase
        test_patch = _test_patch()
        baseline = _baseline_probe(production_image, test_patch, run_id)
        ledger.append({"stage": "baseline", "status": "failed_as_expected", **baseline})
        instance = {
            "instance_id": instance_id,
            "repo": "pytest-dev/pytest",
            "version": "8.3",
            "base_commit": base_commit,
            "problem_statement": (
                f"The add function in {SOURCE_PATH} subtracts its operands. "
                "Change it so add(2, 3) returns 5."
            ),
            "patch": "",
            "test_patch": test_patch,
            "FAIL_TO_PASS": [TARGET_TEST],
            "PASS_TO_PASS": [],
            "image_tag": instance_id,
        }
        remote_root = work / "remote-root"
        _write_dataset(remote_root, instance)
        fake_process, local_base_url = _start_fake_service(artifact_dir)
        remote_port = _free_port()
        remote_base_url = f"http://127.0.0.1:{remote_port}/v1"
        proxy_env = work / "fake-proxy.env"
        proxy_env.write_text(f"OPENCOLLAB_PROXY_CLIENT_TOKEN={FAKE_API_KEY}\n", encoding="utf-8")
        runtime = work / "remote-runtime"
        production_run = work / "production-run"
        report_path = artifact_dir / "report.json"
        markdown_path = artifact_dir / "report.md"
        command = _production_command(
            executable=args.python.parent / "oc-eval",
            ssh_command=args.ssh_command,
            host=args.ssh_host,
            remote_root=remote_root,
            runtime=runtime,
            run_dir=production_run,
            run_id=run_id,
            image_repository=image_repository,
            local_base_url=local_base_url,
            remote_base_url=remote_base_url,
            proxy_env=proxy_env,
            json_output=report_path,
            markdown_output=markdown_path,
        )
        phase = time.monotonic()
        try:
            production = _run(command, timeout=360, env=_clean_environment())
        except BaseException:
            diagnostics = artifact_dir / "failure-diagnostics"
            for source in production_run.rglob("*"):
                if source.is_file() and source.suffix in {".json", ".jsonl", ".log"}:
                    destination = diagnostics / source.relative_to(production_run)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
            raise
        production_seconds = time.monotonic() - phase
        (artifact_dir / "production-command.log").write_text(
            production.stdout + production.stderr, encoding="utf-8"
        )
        report = _json(report_path)
        markdown = markdown_path.read_text(encoding="utf-8")
        prediction_path = production_run / instance_id / "predictions.jsonl"
        metric_path = production_run / instance_id / "metrics.jsonl"
        prediction = _jsonl_one(prediction_path)
        metric = _jsonl_one(metric_path)
        shutil.copy2(prediction_path, artifact_dir / "prediction.jsonl")
        shutil.copy2(metric_path, artifact_dir / "metrics.jsonl")
        report, markdown, production_evidence = publish_production_evidence(
            report,
            markdown,
            artifact_dir=artifact_dir,
            production_run=production_run,
        )
        _write_json(report_path, report)
        markdown_path.write_text(markdown, encoding="utf-8")
        candidate = _validate_candidate(
            prediction, metric, report, instance_id=instance_id, run_id=run_id
        )
        patch = prediction["model_patch"]
        (artifact_dir / "candidate.patch").write_text(patch, encoding="utf-8")
        _write_json(artifact_dir / "candidate-proof.json", candidate)
        ledger.append({"stage": "production_command", "status": "verified"})
        phase = time.monotonic()
        official = _official_eval(
            instance,
            prediction,
            architecture=architecture,
            namespace=namespace,
            run_id=run_id,
            workspace=work / "official",
            artifact_dir=artifact_dir,
        )
        official_seconds = time.monotonic() - phase
        _validate_terminal_report(report, markdown, official, instance_id=instance_id)
        if not _trace_proves_clean_environment(artifact_dir / "fake-model-trace.jsonl"):
            raise RuntimeError("fake model trace does not prove a clean deterministic environment")
        evidence = {
            "schema": "opencollab.deterministic_swe_e2e_validation.v2",
            "run_id": run_id,
            "instance_id": instance_id,
            "record_id": prediction["record_id"],
            "patch_sha256": prediction["patch_sha256"],
            "runtime_tree_sha256": metric["runtime_tree_sha256"],
            "production_report_sha256": _sha256_file(report_path),
            "production_markdown_sha256": _sha256_file(markdown_path),
            "production_evidence_index_sha256": _sha256_file(
                artifact_dir / "production-evidence-index.json"
            ),
            "production_evidence_files": production_evidence["files"],
            "official_report_sha256": official["report_sha256"],
            "resolved": report["counts"]["resolved"],
            "unresolved": report["counts"]["unresolved"],
            "technical_failed": report["counts"]["technical_failed"],
            "target_test_executed": official["target_passed"],
            "provider_environment_clean": True,
            "stage_timings": {
                "image_build_seconds": image_seconds,
                "production_command_seconds": production_seconds,
                "official_eval_seconds": official_seconds,
            },
            "total_seconds": time.monotonic() - started,
            "ledger": ledger,
        }
        _write_json(artifact_dir / "validation.json", evidence)
        return evidence
    except BaseException as exc:
        _write_json(
            artifact_dir / "technical-failure.json",
            {
                "schema": "opencollab.deterministic_swe_e2e_failure.v2",
                "run_id": run_id,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "ledger": ledger,
            },
        )
        raise
    finally:
        if fake_process is not None:
            cleanup["fake_model_stopped"] = _stop_fake_service(fake_process)
        cleanup.update(_cleanup_docker(run_id, images))
        if work.exists():
            shutil.rmtree(work)
        cleanup["temporary_work_removed"] = not work.exists()
        trace_path = artifact_dir / "fake-model-trace.jsonl"
        cleanup["provider_environment_clean"] = (
            trace_path.exists() and _trace_proves_clean_environment(trace_path)
        )
        cleanup["completed_at_ns"] = time.time_ns()
        _write_json(artifact_dir / "cleanup.json", cleanup)
        required = (
            "fake_model_stopped",
            "owned_containers_removed",
            "owned_images_removed",
            "temporary_work_removed",
        )
        if sys.exc_info()[0] is None and not all(cleanup.get(name) is True for name in required):
            raise RuntimeError("deterministic E2E cleanup verification failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--ssh-command", required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    previous = {}

    def interrupted(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous[signum] = signal.signal(signum, interrupted)
    try:
        summary = run_scenario(args)
        print(json.dumps(summary, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
