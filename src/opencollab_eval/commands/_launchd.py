from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

Launchctl = Callable[..., subprocess.CompletedProcess[str]]


def launchctl(*arguments: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["launchctl", *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"launchctl {' '.join(arguments)} failed: {detail}")
    return result


def bootstrap_launch_agent(
    *,
    target: str,
    installed_path: Path,
    launchctl: Launchctl,
    attempts: int = 3,
    delay_seconds: float = 0.5,
) -> None:
    domain = f"gui/{os.getuid()}"
    last_detail = ""
    for attempt in range(attempts):
        result = launchctl("bootstrap", domain, str(installed_path))
        if result.returncode == 0 or launchctl("print", target).returncode == 0:
            return
        last_detail = result.stderr.strip() or result.stdout.strip()
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    suffix = f": {last_detail}" if last_detail else ""
    raise RuntimeError(
        f"launchctl bootstrap {domain} {installed_path} "
        f"failed after {attempts} attempts{suffix}"
    )
