from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from opencollab_eval.commands import swe_g11_parallel_process as process


def test_active_task_cleanup_terminates_the_owned_process_group(tmp_path):
    child_pid_path = tmp_path / "child.pid"
    code = (
        "import pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)"
    )
    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            process.run_task_process(
                [sys.executable, "-c", code, str(child_pid_path)], cwd=tmp_path
            )
        )
    )
    worker.start()
    deadline = time.monotonic() + 5
    while not child_pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_pid_path.exists()

    process.TERM_GRACE_SECONDS = 1
    process.KILL_GRACE_SECONDS = 1
    process.terminate_active_task_groups()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert result[0].returncode != 0
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("task process-group descendant survived scheduler cleanup")


def _assert_sigterm_cleans_driver(source: str, marker: Path, tmp_path: Path) -> None:
    repo = Path(__file__).parents[1]
    parent = subprocess.Popen(
        [sys.executable, "-c", source],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(repo / "src")},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    os.kill(parent.pid, signal.SIGTERM)
    stdout, stderr = parent.communicate(timeout=10)
    assert parent.returncode == 128 + signal.SIGTERM, (stdout, stderr)
    for pid in map(int, marker.read_text(encoding="utf-8").split()):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail(f"process {pid} survived scheduler SIGTERM cleanup")


def test_parallel_sigterm_cleans_children_before_executor_shutdown(tmp_path):
    marker = tmp_path / "pids.txt"
    driver = tmp_path / "driver.py"
    driver.write_text(
        "\n".join(
            [
                "import sys",
                "from pathlib import Path",
                "from types import SimpleNamespace",
                "from opencollab_eval.commands import swe_g11_parallel_runner as runner",
                "from opencollab_eval.commands import swe_g11_parallel_process as process",
                f"marker = Path({str(marker)!r})",
                "code = (\"import os,pathlib,subprocess,sys,time; \"",
                "        \"child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); \"",
                "        \"pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {child.pid}'); time.sleep(60)\")",
                "runner.prepare_runtime = lambda cfg: None",
                "runner.run_remote_health_checks = lambda cfg: {'status': 'skipped'}",
                "runner.run_remote_model_probe = lambda cfg: {'status': 'skipped'}",
                "runner.run_one = lambda cfg, index: process.run_task_process(",
                "    [sys.executable, '-c', code, str(marker)], cwd=marker.parent)",
                "cfg = SimpleNamespace(output_dir=marker.parent, indices=(1,),",
                "    max_workers=1, no_sync_runtime=True, no_ensure_remote_proxy=True)",
                "runner.run_parallel(cfg)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _assert_sigterm_cleans_driver(driver.read_text(encoding="utf-8"), marker, tmp_path)


def test_parallel_sigterm_cleans_preflight_controller_group(tmp_path):
    marker = tmp_path / "preflight-pids.txt"
    source = "\n".join(
        [
            "import sys",
            "from pathlib import Path",
            "from types import SimpleNamespace",
            "from opencollab_eval.commands import swe_g11_parallel_runner as runner",
            "from opencollab_eval.commands import swe_g11_parallel_process as process",
            f"marker = Path({str(marker)!r})",
            "code = (\"import os,pathlib,subprocess,sys,time; \"",
            "        \"child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); \"",
            "        \"pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {child.pid}'); time.sleep(60)\")",
            "real_run = process.run_task_process",
            "runner._run_task_process = lambda command: real_run(",
            "    [sys.executable, '-c', code, str(marker)], cwd=marker.parent)",
            "cfg = SimpleNamespace(skip_preflight=False, output_dir=marker.parent,",
            "    remote_base='/remote/run', host='host', ssh_command='ssh',",
            "    remote_python='python3',",
            "    remote_root='/remote', image_repository='image', run_id='run',",
            "    session_prefix='session', model_name='model', llm_provider='openai',",
            "    indices=(1,), remote_runtime_repo='/remote/runtime', workflow='workflow',",
            "    remote_proxy_base_url='http://remote', budget=1, max_steps=1,",
            "    swe_timeout=14400, task_wall_timeout=15300, eval_timeout=7200,",
            "    llm_timeout=900, checkpoint_interval=0, total_timeout=240000,",
                "    openhands_empty_patch_rejections=0, max_empty_patch_retries=0,",
                "    provider_error_time_budget=0,",
                "    max_task_starts=1, max_eval_attempts=1,",
            "    remote_api_env_file='/secret', llm_model='kimi-for-coding',",
            "    context_window=262144, temperature=1.0, top_p=0.95,",
            "    max_output_tokens=32768, workflow_env=[], openhands_command='',",
            "    no_sync_runtime=True, runtime_tree_sha256='a' * 64,",
            "    no_ensure_remote_proxy=True, max_workers=1)",
            "runner.run_parallel(cfg)",
        ]
    )

    _assert_sigterm_cleans_driver(source, marker, tmp_path)


def test_signal_handler_never_reenters_the_active_process_lock(tmp_path):
    marker = tmp_path / "locked-child.pid"
    source = "\n".join(
        [
            "import os, signal, subprocess, sys",
            "from opencollab_eval.commands import swe_g11_parallel_process as process",
            f"marker = {str(marker)!r}",
            "process.install_signal_handlers()",
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)",
            "open(marker, 'w').write(str(child.pid))",
            "with process._ACTIVE_LOCK:",
            "    process._ACTIVE_PROCESSES.add(child)",
            "    process._ACTIVE_SNAPSHOT = (child,)",
            "    os.kill(os.getpid(), signal.SIGTERM)",
        ]
    )
    proc = subprocess.run(
        [sys.executable, "-c", source],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        text=True,
        capture_output=True,
        timeout=3,
    )
    assert proc.returncode == 128 + signal.SIGTERM, (proc.stdout, proc.stderr)
    child_pid = int(marker.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("active child survived a signal delivered while its lock was held")


def test_spawn_signal_is_deferred_until_the_process_is_owned(tmp_path):
    marker = tmp_path / "spawned-child.pid"
    source = "\n".join(
        [
            "import os, signal, sys",
            "from pathlib import Path",
            "from opencollab_eval.commands import swe_g11_parallel_process as process",
            "process.install_signal_handlers()",
            "real_popen = process.subprocess.Popen",
            "def injected_popen(*args, **kwargs):",
            "    child = real_popen(*args, **kwargs)",
            f"    Path({str(marker)!r}).write_text(str(child.pid))",
            "    os.kill(os.getpid(), signal.SIGTERM)",
            "    return child",
            "process.subprocess.Popen = injected_popen",
            "process.run_task_process([sys.executable, '-c', 'import time; time.sleep(60)'], cwd=Path.cwd())",
        ]
    )
    proc = subprocess.run(
        [sys.executable, "-c", source],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        text=True,
        capture_output=True,
        timeout=3,
    )
    assert proc.returncode == 128 + signal.SIGTERM, (proc.stdout, proc.stderr)
    child_pid = int(marker.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("child spawned during deferred SIGTERM survived ownership cleanup")
