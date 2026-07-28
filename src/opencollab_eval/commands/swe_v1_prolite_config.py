"""Configuration, proxy setup, and runtime sync for the pro-lite launcher."""

from __future__ import annotations

import argparse
import atexit
import hashlib
import importlib
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import tarfile
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from opencollab_eval.commands.swe_ssh_transport import run_checked, run_ssh_checked
from opencollab_eval.commands.swe_v1_prolite_common import (
    ALLOWED_WORKFLOW_ENV_KEYS,
    DEFAULT_BASE_RUN_DIR_PREFIX,
    MAX_PROXY_ENV_BYTES,
    PACKAGE_ROOT,
    PROXY_PROCESS_LOOKUP_TIMEOUT_SECONDS,
    REMOTE_HEALTH_SSH_TIMEOUT_FLOOR,
    REMOTE_PROXY_TUNNELS,
    REPO_ROOT,
    SYNC_DIRS,
    SYNC_FILES,
    _redacted,
)
from opencollab_eval.commands.swe_v1_prolite_process import (
    _block_local_spawn_signals,
    _ensure_local_process_group_quiesced_after_wait,
    _restore_local_spawn_signals,
    terminate_local_process_group,
)

RUNTIME_IMPORT_PROBES = (
    "opencollab_eval.generation.gen_prediction_openhands",
    "opencollab_eval.generation.gen_prediction_workflow",
    "opencollab_eval.engine.evaluator",
)
RUNTIME_PUBLIC_MODULES = (
    "__init__.py",
    "environments.py",
    "tools.py",
    "workflows.py",
)
MIN_OPENCOLLAB_RELEASE = (0, 4, 1)

def verify_runtime_import_contract() -> None:
    """Import runtime entrypoints so an incomplete SDK fails before task launch."""
    for module_name in RUNTIME_IMPORT_PROBES:
        try:
            importlib.import_module(module_name)
        except (ImportError, AttributeError) as exc:
            raise RuntimeError(
                f"OpenCollab public API is incompatible with OpenCollab-Eval: "
                f"cannot import {module_name}: {type(exc).__name__}: {exc}"
            ) from exc


def verify_runtime_public_api_layout(root: str | Path) -> None:
    """Verify the public modules required by remote entrypoints."""
    package_root = Path(root) / "src" / "opencollab"
    invalid = []
    for module_name in RUNTIME_PUBLIC_MODULES:
        path = package_root / module_name
        try:
            info = path.lstat()
        except OSError:
            invalid.append(module_name.removesuffix(".py"))
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
            invalid.append(module_name.removesuffix(".py"))
    if invalid:
        raise RuntimeError(
            "synchronized OpenCollab public API is missing required modules: "
            + ", ".join(invalid)
        )


def _runtime_input_path(relative_path: str) -> Path:
    package_prefix = "src/opencollab_eval"
    if relative_path == package_prefix:
        return PACKAGE_ROOT
    if relative_path.startswith(package_prefix + "/"):
        return PACKAGE_ROOT / relative_path.removeprefix(package_prefix + "/")
    return REPO_ROOT / relative_path


def _runtime_directory_sources() -> tuple[dict[str, Path], str]:
    sources = {relative: _runtime_input_path(relative) for relative in SYNC_DIRS}
    package = importlib.import_module("opencollab")
    sources["src/opencollab"] = Path(next(iter(package.__path__))).resolve()
    try:
        distribution_version = version("opencollab")
    except PackageNotFoundError as exc:
        raise RuntimeError("the OpenCollab distribution metadata is missing") from exc
    release_match = re.match(r"^(\d+)\.(\d+)\.(\d+)", distribution_version)
    release = tuple(map(int, release_match.groups())) if release_match else ()
    if release < MIN_OPENCOLLAB_RELEASE or release >= (0, 5, 0):
        raise RuntimeError(f"OpenCollab 0.4.1 or newer within the 0.4 series is required, found {distribution_version}")
    if getattr(package, "__version__", None) != distribution_version:
        raise RuntimeError("the imported OpenCollab source version does not match its distribution metadata")
    verify_runtime_import_contract()
    return sources, distribution_version


def _runtime_source_entries(
    files: list[str], directory_sources: dict[str, Path]
) -> list[tuple[str, Path]]:
    entries = {relative: _runtime_input_path(relative) for relative in files}
    for relative_root, source_root in directory_sources.items():
        for current_root, directories, names in os.walk(source_root, followlinks=False):
            current = Path(current_root)
            directories[:] = sorted(name for name in directories if name != "__pycache__")
            for name in tuple(directories):
                path = current / name
                if path.is_symlink():
                    directories.remove(name)
                    entries[str(Path(relative_root) / path.relative_to(source_root))] = path
            for name in sorted(names):
                if name.endswith((".pyc", ".pyo")):
                    continue
                path = current / name
                entries[str(Path(relative_root) / path.relative_to(source_root))] = path
    return sorted(entries.items())


def _runtime_entry_record(relative: str, path: Path) -> tuple[bytes, int]:
    info = path.lstat()
    name = relative.encode("utf-8", errors="surrogatepass")
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path).encode("utf-8", errors="surrogatepass")
        return b"L\0" + name + b"\0" + target + b"\n", 0
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"runtime input must be a regular file or symlink: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    executable = b"1" if info.st_mode & 0o111 else b"0"
    return b"F\0" + name + b"\0" + executable + b"\0" + digest.hexdigest().encode() + b"\n", size


def runtime_tree_identity(root: Path, members: list[str]) -> dict[str, Any]:
    """Return the canonical identity of the declared runtime source files."""
    digest = hashlib.sha256()
    total_bytes = 0
    seen: set[str] = set()
    for relative in members:
        if relative in seen or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise RuntimeError("runtime manifest contains an invalid member path")
        seen.add(relative)
        record, size = _runtime_entry_record(relative, root / relative)
        digest.update(record)
        total_bytes += size
    return {
        "schema": "opencollab.runtime_tree.v1",
        "sha256": digest.hexdigest(),
        "file_count": len(members),
        "source_bytes": total_bytes,
    }


def verify_runtime_manifest(root: str | Path) -> dict[str, Any]:
    """Verify a synchronized runtime against its declared complete source tree."""
    root = Path(root)
    manifest_path = root / "runtime-manifest.json"
    info = manifest_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
        raise RuntimeError("runtime manifest must be a bounded regular file")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    members = manifest.get("archive_members")
    expected = manifest.get("source_tree")
    if manifest.get("version") != 2 or not isinstance(members, list) or not isinstance(expected, dict):
        raise RuntimeError("runtime manifest has an unsupported shape")
    observed = runtime_tree_identity(root, members)
    if observed != expected:
        raise RuntimeError("synchronized runtime source tree does not match its manifest")
    verify_runtime_public_api_layout(root)
    return observed


def verify_remote_runtime(
    *,
    ssh_command: list[str],
    host: str,
    remote_runtime_repo: str,
    expected: dict[str, Any] | None,
    remote_python: str = "python3",
) -> dict[str, Any]:
    """Re-read and verify the installed remote runtime tree over SSH."""
    probe = (
        "import json,sys; "
        "from opencollab_eval.commands.swe_v1_prolite_config import verify_runtime_manifest; "
        "print(json.dumps(verify_runtime_manifest(sys.argv[1]), sort_keys=True))"
    )
    command = (
        "cd "
        + shlex.quote(remote_runtime_repo)
        + " && PYTHONPATH=src "
        + shlex.quote(remote_python)
        + " -c "
        + shlex.quote(probe)
        + " "
        + shlex.quote(remote_runtime_repo)
    )
    result = run_checked([*ssh_command, host, command], timeout=120)
    try:
        observed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("remote runtime verification returned invalid JSON") from exc
    if expected is not None and observed != expected:
        raise RuntimeError("installed remote runtime source tree does not match the local source tree")
    return observed


def normalize_workflow_env(
    values: list[str] | tuple[str, ...],
) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for item in values:
        key, separator, value = str(item).partition("=")
        if not separator or key not in ALLOWED_WORKFLOW_ENV_KEYS:
            raise ValueError(f"unsupported --workflow-env: {item}")
        normalized[key] = value
    return normalized


def _read_bounded_regular_text(path: Path, *, max_bytes: int) -> str:
    path = path.expanduser()
    try:
        before = path.lstat()
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        raise RuntimeError(f"input must be a bounded regular file: {path}")
    fd = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(fd)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise RuntimeError(f"input changed while opening: {path}")
        raw = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise RuntimeError(f"input exceeds {max_bytes} bytes: {path}")
    return raw.decode("utf-8")


def load_shell_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    text = _read_bounded_regular_text(path, max_bytes=MAX_PROXY_ENV_BYTES)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.replace("_", "").isalnum():
            continue
        parsed = shlex.split(value, posix=True)
        values[key] = parsed[0] if parsed else ""
    return values


def token_from_values(values: dict[str, str]) -> str:
    for name in (
        "OPENCOLLAB_PROXY_CLIENT_TOKEN",
        "GLM_PROXY_CLIENT_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENCOLLAB_API_KEY",
    ):
        value = values.get(name)
        if value:
            return value
    return ""


def token_from_env_file(path: Path) -> str:
    try:
        return token_from_values(load_shell_env(path))
    except FileNotFoundError:
        return ""


def proxy_env_file_from_ps(ps_text: str) -> Path | None:
    try:
        parts = shlex.split(ps_text)
    except ValueError:
        return None
    for index, part in enumerate(parts):
        if part == "--env-file" and index + 1 < len(parts):
            return Path(parts[index + 1])
        if part.startswith("--env-file="):
            return Path(part.split("=", 1)[1])
    return None


def get_proxy_token(proxy_env_file: Path | None) -> str:
    token = token_from_values(dict(os.environ))
    if token:
        return token
    if proxy_env_file is not None:
        token = token_from_env_file(proxy_env_file)
        if token:
            return token
    try:
        pids = subprocess.check_output(
            [
                "pgrep",
                "-f",
                "opencollab_glm_anthropic_proxy.py|glm_anthropic_proxy.py",
            ],
            text=True,
            timeout=PROXY_PROCESS_LOOKUP_TIMEOUT_SECONDS,
        ).split()
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("timed out while locating the glm proxy process") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("glm proxy process not found") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to locate the glm proxy process: {exc}") from exc
    if not pids:
        raise RuntimeError("glm proxy process not found")
    try:
        ps = subprocess.check_output(
            ["ps", "eww", "-p", pids[0]],
            text=True,
            timeout=PROXY_PROCESS_LOOKUP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("timed out while reading the glm proxy environment") from exc
    except (subprocess.CalledProcessError, OSError) as exc:
        raise RuntimeError(f"failed to read the glm proxy environment: {exc}") from exc
    env_path = proxy_env_file_from_ps(ps)
    if env_path:
        token = token_from_env_file(env_path)
        if token:
            return token
    match = re.search(r"GLM_PROXY_CLIENT_TOKEN=(\S+)", ps)
    if not match:
        raise RuntimeError("proxy token not found in environment, proxy env file, or proxy process")
    return match.group(1)


def url_with_healthz(base_url: str) -> str:
    return base_url.rstrip("/") + "/healthz"


def local_http_ok(base_url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url_with_healthz(base_url), timeout=timeout) as response:
            return 200 <= response.status < 400
    except Exception:
        return False


def remote_http_ok(
    *, ssh_command: list[str], host: str, base_url: str, remote_python: str = "python3", timeout: int = 10
) -> bool:
    probe = "import sys,urllib.request;urllib.request.urlopen(sys.argv[1], timeout=" + str(timeout) + ").read()"
    remote_command = f"{shlex.quote(remote_python)} -c {shlex.quote(probe)} {shlex.quote(url_with_healthz(base_url))}"
    try:
        result = subprocess.run(
            [*ssh_command, host, remote_command],
            text=True,
            capture_output=True,
            timeout=max(REMOTE_HEALTH_SSH_TIMEOUT_FLOOR, timeout + 8),
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def loopback_port(base_url: str, *, default: int | None = None) -> int:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"proxy URL must be loopback: {base_url}")
    if parsed.port is None:
        if default is None:
            raise RuntimeError(f"proxy URL must include an explicit port: {base_url}")
        return int(default)
    return int(parsed.port)


def loopback_url_with_port(base_url: str, port: int) -> str:
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"proxy URL must be loopback: {base_url}")
    if host == "::1":
        netloc = f"[::1]:{port}"
    else:
        netloc = f"{host}:{port}"
    return urllib.parse.urlunparse(parsed._replace(netloc=netloc))


def remote_forward_port_conflict(message: str) -> bool:
    lowered = message.lower()
    return (
        "remote port forwarding failed" in lowered
        or "address already in use" in lowered
        or "cannot listen to port" in lowered
    )


def stop_remote_proxy_tunnel(proc: subprocess.Popen[str]) -> bool:
    return terminate_local_process_group(proc)


def cleanup_remote_proxy_tunnels() -> None:
    for proc in list(REMOTE_PROXY_TUNNELS):
        try:
            cleanup_quiesced = stop_remote_proxy_tunnel(proc)
        except BaseException:
            cleanup_quiesced = False
        if cleanup_quiesced and proc in REMOTE_PROXY_TUNNELS:
            REMOTE_PROXY_TUNNELS.remove(proc)


atexit.register(cleanup_remote_proxy_tunnels)


def start_remote_proxy_tunnel(command: list[str]) -> tuple[subprocess.Popen[str] | None, str]:
    spawn_signal_state = _block_local_spawn_signals()
    try:
        proc = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except BaseException:
        _restore_local_spawn_signals(spawn_signal_state)
        raise
    REMOTE_PROXY_TUNNELS.append(proc)
    try:
        _restore_local_spawn_signals(spawn_signal_state)
        time.sleep(0.2)
        if proc.poll() is not None:
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                cleanup_quiesced = terminate_local_process_group(proc)
                if cleanup_quiesced and proc in REMOTE_PROXY_TUNNELS:
                    REMOTE_PROXY_TUNNELS.remove(proc)
                return None, "ssh tunnel output drain timed out"
            cleanup_quiesced = _ensure_local_process_group_quiesced_after_wait(proc)
            if cleanup_quiesced:
                REMOTE_PROXY_TUNNELS.remove(proc)
            else:
                return (
                    None,
                    "ssh tunnel leader exited with residual process-group descendants that could not be cleaned",
                )
            message = _redacted(stderr or stdout or f"{command[0]} exited {proc.returncode}")
            return None, message
        return proc, ""
    except BaseException:
        cleanup_quiesced = False
        try:
            cleanup_quiesced = terminate_local_process_group(proc)
        except BaseException:
            pass
        if cleanup_quiesced and proc in REMOTE_PROXY_TUNNELS:
            REMOTE_PROXY_TUNNELS.remove(proc)
        raise


def ensure_remote_proxy(
    *,
    ssh_command: list[str],
    host: str,
    local_proxy_base_url: str,
    remote_proxy_base_url: str,
    remote_python: str = "python3",
    enabled: bool,
) -> dict[str, Any]:
    if not enabled:
        return {"status": "disabled"}
    if remote_http_ok(ssh_command=ssh_command, host=host, base_url=remote_proxy_base_url, remote_python=remote_python):
        return {"status": "already_healthy", "remote_proxy_base_url": remote_proxy_base_url}
    if not local_http_ok(local_proxy_base_url):
        raise RuntimeError(f"local proxy health check failed: {url_with_healthz(local_proxy_base_url)}")
    local_port = loopback_port(local_proxy_base_url)
    remote_port = loopback_port(remote_proxy_base_url)
    attempts: list[str] = []
    for candidate_port in range(remote_port, remote_port + 21):
        candidate_base_url = loopback_url_with_port(remote_proxy_base_url, candidate_port)
        forward = f"127.0.0.1:{candidate_port}:127.0.0.1:{local_port}"
        command = [
            *ssh_command,
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-R",
            forward,
            host,
        ]
        proc, message = start_remote_proxy_tunnel(command)
        if proc is None:
            attempts.append(f"{candidate_port}: {message}")
            if remote_forward_port_conflict(message):
                if remote_http_ok(
                    ssh_command=ssh_command, host=host, base_url=candidate_base_url,
                    remote_python=remote_python, timeout=2,
                ):
                    return {
                        "status": "already_healthy",
                        "remote_proxy_base_url": candidate_base_url,
                        "selected_remote_port": candidate_port,
                    }
                continue
            raise RuntimeError(message)
        for _ in range(6):
            if remote_http_ok(
                ssh_command=ssh_command, host=host, base_url=candidate_base_url,
                remote_python=remote_python, timeout=2,
            ):
                return {
                    "status": "started" if candidate_port == remote_port else "started_fallback_port",
                    "local_proxy_base_url": local_proxy_base_url,
                    "remote_proxy_base_url": candidate_base_url,
                    "forward": forward,
                    "selected_remote_port": candidate_port,
                }
            time.sleep(0.5)
        cleanup_quiesced = stop_remote_proxy_tunnel(proc)
        if cleanup_quiesced and proc in REMOTE_PROXY_TUNNELS:
            REMOTE_PROXY_TUNNELS.remove(proc)
        if not cleanup_quiesced:
            raise RuntimeError(f"remote proxy tunnel on port {candidate_port} did not stop")
        attempts.append(f"{candidate_port}: tunnel started but health check failed")
    detail = "; ".join(attempts[-5:])
    raise RuntimeError(f"remote proxy tunnel did not become healthy near port {remote_port}: {detail}")


def sync_runtime(
    *,
    ssh_command: list[str],
    host: str,
    remote_runtime_repo: str,
    remote_python: str = "python3",
) -> dict[str, Any]:
    synced = list(SYNC_FILES)
    directory_sources, distribution_version = _runtime_directory_sources()
    synced_dirs = list(directory_sources)
    missing = [
        rel
        for rel in synced
        if not _runtime_input_path(rel).is_file()
    ]
    missing.extend(
        rel for rel, local_path in directory_sources.items() if not local_path.is_dir()
    )
    if missing:
        raise RuntimeError("required runtime inputs are missing: " + ", ".join(sorted(missing)))
    source_entries = _runtime_source_entries(synced, directory_sources)
    archive_members = [relative for relative, _path in source_entries]
    digest = hashlib.sha256()
    source_bytes = 0
    for relative, path in source_entries:
        record, size = _runtime_entry_record(relative, path)
        digest.update(record)
        source_bytes += size
    source_tree = {
        "schema": "opencollab.runtime_tree.v1",
        "sha256": digest.hexdigest(),
        "file_count": len(source_entries),
        "source_bytes": source_bytes,
    }

    target = remote_runtime_repo.rstrip("/")
    if not target or target in {".", "..", "/"}:
        raise ValueError("remote_runtime_repo must identify a dedicated runtime directory")
    target_parent = str(Path(target).parent)
    token = secrets.token_hex(8)
    remote_archive = f"{target}.archive.{token}.tgz"
    remote_staging = f"{target}.staging.{token}"
    remote_backup = f"{target}.backup.{token}"
    ssh_part = " ".join(shlex.quote(part) for part in ssh_command)
    with tempfile.TemporaryDirectory(prefix="swe-v1-runtime-") as tmp_dir:
        archive_path = Path(tmp_dir) / "runtime.tgz"
        manifest_path = Path(tmp_dir) / "runtime-manifest.json"

        def archive_filter(tar_info: tarfile.TarInfo) -> tarfile.TarInfo | None:
            parts = Path(tar_info.name).parts
            if "__pycache__" in parts or tar_info.name.endswith((".pyc", ".pyo")):
                return None
            return tar_info

        with tarfile.open(archive_path, "w:gz") as archive:
            for rel in synced:
                local_path = _runtime_input_path(rel)
                archive.add(local_path, arcname=rel, filter=archive_filter)
            for rel in synced_dirs:
                local_path = directory_sources[rel]
                archive.add(local_path, arcname=rel, filter=archive_filter)
            manifest = {
                "version": 2,
                "synced": synced,
                "synced_dirs": synced_dirs,
                "opencollab": {
                    "distribution_version": distribution_version,
                    "public_api_version": 1,
                },
                "archive_members": archive_members,
                "source_tree": source_tree,
            }
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            archive.add(manifest_path, arcname="runtime-manifest.json")

        mkdir_attempts: list[dict[str, object]] = []
        run_ssh_checked(
            [*ssh_command, host, "mkdir -p -- " + shlex.quote(target_parent)],
            timeout=60, retry_log=mkdir_attempts,
        )
        transfer_command = [
            "rsync", "-az", "--partial", "-e", ssh_part,
            str(archive_path), f"{host}:{remote_archive}",
        ]
        transfer_attempts: list[dict[str, object]] = []
        try:
            run_ssh_checked(
                transfer_command, timeout=300, retry_log=transfer_attempts,
            )
        except (RuntimeError, subprocess.TimeoutExpired):
            try:
                run_ssh_checked(
                    [*ssh_command, host, "rm -f -- " + shlex.quote(remote_archive)],
                    timeout=60,
                )
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
            raise
    sh_files = [rel for rel in synced if rel.endswith(".sh")]
    compile_targets = [
        rel
        for rel in ("scripts", "swebench", "workflows", *SYNC_DIRS)
        if rel in synced_dirs or any(item == rel or item.startswith(rel + "/") for item in synced)
    ]
    install_lines = [
        "set -eu",
        "target=" + shlex.quote(target),
        "stage=" + shlex.quote(remote_staging),
        "backup=" + shlex.quote(remote_backup),
        "archive=" + shlex.quote(remote_archive),
        "target_moved=0",
        "installed=0",
        "cleanup() {",
        '  if [ "$target_moved" -eq 1 ] && [ "$installed" -eq 0 ] && '
        '     { [ ! -e "$target" ] && [ ! -L "$target" ]; }; then',
        '    mv -- "$backup" "$target" || true',
        "  fi",
        '  rm -rf -- "$stage"',
        '  rm -f -- "$archive"',
        "}",
        "trap cleanup EXIT HUP INT TERM",
        'if [ -e "$target" ] || [ -L "$target" ]; then',
        '  current_manifest="$target/runtime-manifest.json"',
        '  legacy_archive="$target/runtime.tgz"',
        '  if { [ ! -f "$current_manifest" ] || [ -L "$current_manifest" ]; } &&',
        '     { [ ! -f "$legacy_archive" ] || [ -L "$legacy_archive" ]; }; then',
        '    echo "refusing to replace an unmarked runtime directory: $target" >&2',
        "    exit 1",
        "  fi",
        "fi",
        'test ! -e "$stage" && test ! -L "$stage"',
        'test ! -e "$backup" && test ! -L "$backup"',
        'mkdir -- "$stage"',
        'tar -xzf "$archive" -C "$stage"',
    ]
    prepare_commands: list[str] = []
    quoted_remote_python = shlex.quote(remote_python)
    if sh_files:
        prepare_commands.append("chmod +x " + " ".join(shlex.quote(rel) for rel in sh_files))
    if compile_targets:
        prepare_commands.append(
            quoted_remote_python + " -m compileall -q "
            + " ".join(shlex.quote(rel) for rel in compile_targets)
        )
    prepare_commands.append(
        "PYTHONPATH=src " + quoted_remote_python + " -c "
        + shlex.quote(
            "import opencollab, opencollab_eval; "
            "from opencollab import OpenCollab, RunResult; "
            "from opencollab.environments import attach_container; "
            "from opencollab.tools import builtin_tools; "
            "from opencollab.workflows import workflow; "
            f"assert opencollab.__version__=={distribution_version!r}; "
            "assert OpenCollab and RunResult and attach_container and builtin_tools and workflow"
        )
    )
    prepare_commands.append(
        "PYTHONPATH=src " + quoted_remote_python + " -c "
        + shlex.quote(
            "import sys; "
            "from opencollab_eval.commands.swe_v1_prolite_config import verify_runtime_manifest; "
            "observed=verify_runtime_manifest(sys.argv[1]); "
            f"assert observed['sha256']=='{source_tree['sha256']}'"
        )
        + ' "$stage"'
    )
    if prepare_commands:
        install_lines.append('(cd "$stage" && ' + " && ".join(prepare_commands) + ")")
    install_lines.extend(
        [
            'if [ -e "$target" ] || [ -L "$target" ]; then',
            '  mv -- "$target" "$backup"',
            "  target_moved=1",
            "fi",
            'mv -- "$stage" "$target"',
            "installed=1",
            'if [ "$target_moved" -eq 1 ]; then rm -rf -- "$backup"; fi',
            'rm -f -- "$archive"',
            "trap - EXIT HUP INT TERM",
        ]
    )
    install_attempts: list[dict[str, object]] = []
    run_ssh_checked(
        [*ssh_command, host, "\n".join(install_lines)],
        timeout=300, retry_log=install_attempts,
    )
    return {
        "remote_runtime_repo": target,
        "synced": synced,
        "synced_dirs": synced_dirs,
        "compile_targets": compile_targets,
        "remote_python": remote_python,
        "manifest": "runtime-manifest.json",
        "opencollab": {"distribution_version": distribution_version, "public_api_version": 1},
        "source_tree": {
            "local": source_tree,
            "remote": source_tree,
            "verified": True,
        },
        "ssh_transport_attempts": {
            "mkdir": mkdir_attempts,
            "transfer": transfer_attempts,
            "install": install_attempts,
        },
    }


def configure_run_paths(args: argparse.Namespace) -> None:
    if not args.run_id:
        args.run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
    if not args.base_run_dir:
        if DEFAULT_BASE_RUN_DIR_PREFIX:
            prefix = DEFAULT_BASE_RUN_DIR_PREFIX.rstrip("_")
            args.base_run_dir = f"{prefix}_{args.run_id}"
        else:
            args.base_run_dir = str(Path(args.remote_root) / "runs" / f"swe_v1_prolite_{args.run_id}")
    if not args.remote_runtime_repo:
        args.remote_runtime_repo = str(Path(args.base_run_dir) / "_runtime" / "repo")


def validate_run_id(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("run_id must be one non-empty path component")
    if Path(value).is_absolute() or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        raise ValueError("run_id must be one safe path component")
    if len(value.encode("utf-8")) > 240:
        raise ValueError("run_id exceeds 240 UTF-8 bytes")
    return value


__all__ = [name for name in globals() if not name.startswith("__")]
