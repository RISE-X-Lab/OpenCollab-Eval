"""Disposable synthetic repository fixtures for core workspace-integrity tests."""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
from pathlib import Path

OWNER_LABEL = "opencollab.eval.deterministic-e2e"
SOURCE_PATH = "package b/source files/calculator.py"
PROVIDER_KEY_NAMES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DASHSCOPE_API_KEY", "GLM_PROXY_CLIENT_TOKEN",
    "KIMI_API_KEY", "MOONSHOT_API_KEY", "OPENCOLLAB_API_KEY", "OPENCOLLAB_PROXY_CLIENT_TOKEN",
    "OPENCOLLAB_READ_TOKEN", "OPENAI_API_KEY",
)


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
        "RUN python -m venv --system-site-packages /opt/miniconda3/envs/testbed && "
        "mkdir -p /opt/miniconda3/bin && printf '%s\\n' "
        "'conda() { . /opt/miniconda3/envs/testbed/bin/activate; "
        "export CONDA_DEFAULT_ENV=\"${2:-${1:-testbed}}\"; }' "
        "'export -f conda' 'conda activate \"${1:-testbed}\"' > /opt/miniconda3/bin/activate\n"
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


def _build_image(context: Path, tags: list[str]) -> str:
    _run(["docker", "build", "--pull=false", "--tag", tags[0], str(context)], timeout=300)
    for tag in tags[1:]:
        _run(["docker", "tag", tags[0], tag], timeout=30)
    result = _run(["docker", "run", "--rm", "--network", "none", tags[0], "git", "rev-parse", "HEAD"])
    commit = result.stdout.strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise RuntimeError("synthetic image returned an invalid base commit")
    return commit


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
