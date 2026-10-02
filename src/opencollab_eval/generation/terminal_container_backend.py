"""Full-container candidate storage for the released Duo workflow.

Only real filesystem changes are projected as evidence. Selection remains in
the released workflow; adoption retains the selected live container.
"""

from __future__ import annotations

import asyncio
import difflib
import io
import json
import posixpath
import shlex
import subprocess
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def docker(*args, input_bytes=None, timeout=120):
    result = subprocess.run(
        ["docker", *args], input=input_bytes, capture_output=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError(
            f"Docker {args[0]} failed: {result.stderr.decode(errors='replace')[-3000:]}"
        )
    return result.stdout


def inspect(reference):
    return json.loads(docker("inspect", reference))[0]


EXEC_WRAPPER = r"""
pidfile=$1
command=$2
env --default-signal=INT,QUIT setsid /bin/bash -c "$command" <&0 &
child=$!
printf '%s\n' "$child" > "$pidfile" || exit 125
wait "$child"
result=$?
rm -f -- "$pidfile"
exit "$result"
"""

CANCEL_EXEC = r"""
pidfile=$1
for n in 1 2 3 4 5 6 7 8 9 10; do
  [ -s "$pidfile" ] && break
  sleep 0.05
done
[ -s "$pidfile" ] || exit 3
read -r child < "$pidfile"
case "$child" in ''|*[!0-9]*) exit 125 ;; esac
kill -TERM -- "-$child" 2>/dev/null || true
sleep 0.2
kill -KILL -- "-$child" 2>/dev/null || true
"""


class ContainerEnvironment:
    """An externally owned container with cancellable foreground commands."""

    local_filesystem = False
    process_isolated = True
    host_workspace = None

    def __init__(self, container, workspace, artifacts):
        self.container = container
        self.workspace = workspace
        self.source_workspace = workspace
        self.artifacts = Path(artifacts)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self._revoked = False
        self._active = {}
        self._communications = {}
        self._capture = None

    @property
    def revoked(self):
        return self._revoked

    def revoke(self):
        self._revoked = True

    async def setup(self, mount_dir=None):
        return self.workspace

    async def _cancel(self, token, process):
        pidfile = "/tmp/.native-g22-exec-" + token
        stop = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            self.container,
            "/bin/bash",
            "-c",
            CANCEL_EXEC,
            "native-cancel",
            pidfile,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(stop.communicate(), 15)
        # A missing pidfile is benign only if the owned exec has already ended.
        if stop.returncode == 3 and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    "cannot prove cancelled container command has stopped"
                ) from exc
        if stop.returncode not in (0, 3):
            raise RuntimeError("container command cancellation failed")

    async def exec_cmd(self, cmd, timeout=120.0, stdin=None):
        if self.revoked:
            raise RuntimeError("candidate environment was revoked")
        token = uuid.uuid4().hex
        pidfile = "/tmp/.native-g22-exec-" + token
        argv = [
            "docker",
            "exec",
            *(["-i"] if stdin is not None else []),
            "--workdir",
            self.workspace,
            "-e",
            "GIT_AUTHOR_NAME=OpenCollab Agent",
            "-e",
            "GIT_AUTHOR_EMAIL=agent@opencollab.invalid",
            "-e",
            "GIT_COMMITTER_NAME=OpenCollab Agent",
            "-e",
            "GIT_COMMITTER_EMAIL=agent@opencollab.invalid",
            self.container,
            "/bin/bash",
            "-c",
            EXEC_WRAPPER,
            "native-exec",
            pidfile,
            cmd,
        ]
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._active[token] = process
        communication = asyncio.create_task(process.communicate(stdin))
        self._communications[token] = communication
        # An outer tool timeout can finish exec_cmd before the Docker client
        # exits. Retire that exact command when its output and exit are settled.
        communication.add_done_callback(lambda _task: self._retire_finished(token, process))
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communication), timeout
            )
        except asyncio.TimeoutError:
            timed_out = True
            await self._cancel(token, process)
            stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), 15)
        except asyncio.CancelledError:
            await self._cancel(token, process)
            await asyncio.wait_for(asyncio.shield(communication), 15)
            raise
        finally:
            self._retire_finished(token, process)
        result = CommandResult(
            124 if timed_out else process.returncode,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )
        with (self.artifacts / "commands.jsonl").open("a") as f:
            f.write(
                json.dumps(
                    {
                        "command": cmd,
                        "returncode": result.returncode,
                        "timed_out": timed_out,
                        "exec_token": token,
                    }
                )
                + "\n"
            )
        return result

    async def read_file(self, path):
        result = await self.exec_cmd("cat -- " + shlex.quote(path))
        if result.returncode:
            if "No such file or directory" in result.stderr:
                raise FileNotFoundError(path)
            raise OSError(result.stderr)
        return result.stdout

    async def write_file(self, path, content):
        command = "mkdir -p -- " + shlex.quote(posixpath.dirname(path) or ".")
        command += " && cat > " + shlex.quote(path)
        result = await self.exec_cmd(command, timeout=120, stdin=content.encode())
        if result.returncode:
            raise OSError(result.stderr or f"container write failed with {result.returncode}")

    async def write_temp_file(self, content, prefix, suffix=".tmp"):
        result = await self.exec_cmd(
            "mktemp /tmp/" + shlex.quote(prefix + "XXXXXXXX" + suffix)
        )
        if result.returncode:
            raise OSError(result.stderr)
        path = result.stdout.strip()
        await self.write_file(path, content)
        return path

    async def remove_file(self, path):
        result = await self.exec_cmd("rm -f -- " + shlex.quote(path))
        if result.returncode:
            raise OSError(result.stderr)

    def _retire_finished(self, token, process):
        communication = self._communications.get(token)
        if (self._active.get(token) is process and process.returncode is not None
                and communication is not None and communication.done()
                and not communication.cancelled() and communication.exception() is None):
            self._active.pop(token, None)
            self._communications.pop(token, None)

    async def ensure_quiescent(self):
        for token, process in list(self._active.items()):
            self._retire_finished(token, process)
        if self._active:
            raise RuntimeError("candidate still has active foreground commands")

    async def abort(self):
        self.revoke()
        for token, p in list(self._active.items()):
            self._retire_finished(token, p)
        for token, p in list(self._active.items()):
            await self._cancel(token, p)
            await asyncio.wait_for(p.wait(), 15)
            communication = self._communications.get(token)
            if communication is not None:
                await asyncio.wait_for(asyncio.shield(communication), 15)
            self._retire_finished(token, p)

    async def cleanup(self):
        await self.abort()

    async def get_filesystem_diff(self, path=None, stat_only=False):
        if self._capture is None:
            raise RuntimeError("filesystem evidence is unavailable")
        diff = await self._capture()
        if path:
            wanted = posixpath.normpath(posixpath.join(self.workspace, path)).lstrip(
                "/"
            )
            parts = diff.split("diff --git ")
            diff = "".join(
                "diff --git " + p
                for p in parts[1:]
                if p.startswith("a/" + wanted + " ")
                or p.startswith("a/" + wanted + "/")
            )
        if stat_only:
            return "\n".join(
                line for line in diff.splitlines() if line.startswith("diff --git ")
            )
        return diff


FILTER_PATHS = r"""
while IFS= read -r -d '' p; do
  if [ -f "$p" ] || [ -L "$p" ] || [ -d "$p" ]; then printf '%s\0' "$p"; fi
done
"""


def file_snapshot(container, paths, output):
    """Archive only named real files and symlinks; never extract to the host root."""
    output.parent.mkdir(parents=True, exist_ok=True)
    names = b"".join(p.encode() + b"\0" for p in paths)
    filtered = docker(
        "exec", "-i", container, "/bin/bash", "-c", FILTER_PATHS, input_bytes=names
    )
    contents = docker(
        "exec",
        "-i",
        container,
        "tar",
        "--no-recursion",
        "--null",
        "-T",
        "-",
        "-cf",
        "-",
        input_bytes=filtered,
        timeout=300,
    )
    output.write_bytes(contents)
    records = {}
    with tarfile.open(fileobj=io.BytesIO(contents), mode="r:") as tf:
        for member in tf:
            name = "/" + member.name.lstrip("/")
            if ".." in Path(name).parts:
                raise RuntimeError("unsafe filesystem evidence path")
            if member.isfile() or member.islnk():
                records[name] = (member.mode, tf.extractfile(member).read(), "file")
            elif member.issym():
                records[name] = (member.mode, member.linkname.encode(), "symlink")
            elif member.isdir():
                records[name.rstrip("/")] = (member.mode, b"", "directory")
    return records


def state_diff(before, after):
    """Create actual Git-style textual/binary differences without model changes."""
    result = []
    for path in sorted(before.keys() | after.keys()):
        a, b = before.get(path), after.get(path)
        if a == b:
            continue
        relative = path.lstrip("/")
        oldmode = (
            None
            if a is None
            else (
                "120000"
                if a[2] == "symlink"
                else "040000"
                if a[2] == "directory"
                else "100755"
                if a[0] & 0o111
                else "100644"
            )
        )
        newmode = (
            None
            if b is None
            else (
                "120000"
                if b[2] == "symlink"
                else "040000"
                if b[2] == "directory"
                else "100755"
                if b[0] & 0o111
                else "100644"
            )
        )
        header = f"diff --git a/{relative} b/{relative}\n"
        if a is None:
            header += f"new file mode {newmode}\n"
        elif b is None:
            header += f"deleted file mode {oldmode}\n"
        elif oldmode != newmode:
            header += f"old mode {oldmode}\nnew mode {newmode}\n"
        adata = a[1] if a else b""
        bdata = b[1] if b else b""
        if adata == bdata:
            body = ""
        else:
            try:
                if b"\0" in adata or b"\0" in bdata:
                    raise UnicodeDecodeError("utf8", b"\0", 0, 1, "binary")
                at, bt = adata.decode(), bdata.decode()
                diff_lines = difflib.unified_diff(
                    at.splitlines(True),
                    bt.splitlines(True),
                    fromfile="a/" + relative if a else "/dev/null",
                    tofile="b/" + relative if b else "/dev/null",
                )
                body = "".join(
                    line
                    if line.endswith("\n")
                    else line + "\n\\ No newline at end of file\n"
                    for line in diff_lines
                )
            except UnicodeDecodeError:
                with tempfile.TemporaryDirectory(prefix="g22-binary-diff-") as temp:
                    ap, bp = Path(temp) / "before", Path(temp) / "after"
                    attrs = Path(temp) / ".gitattributes"
                    ap.write_bytes(adata)
                    bp.write_bytes(bdata)
                    attrs.write_text("* -diff\n")
                    p = subprocess.run(
                        [
                            "git", "-c", f"core.attributesFile={attrs}", "diff",
                            "--no-index", "--binary", "--",
                            str(ap) if a else "/dev/null",
                            str(bp) if b else "/dev/null",
                        ],
                        capture_output=True,
                    )
                    if p.returncode not in (0, 1):
                        raise RuntimeError(p.stderr.decode(errors="replace")) from None
                    text = p.stdout.decode()
                    marker = text.find("GIT binary patch")
                    if marker < 0:
                        raise RuntimeError("binary difference omitted its real payload") from None
                    index = next(
                        (line for line in text.splitlines() if line.startswith("index ")),
                        None,
                    )
                    if index is None:
                        raise RuntimeError("binary difference omitted its blob identities") from None
                    blob_pair = index.split()[1]
                    mode = f" {newmode}" if oldmode == newmode else ""
                    body = f"index {blob_pair}{mode}\n" + text[marker:]
        if a is not None and b is not None and a[0] != b[0]:
            header += f"observed old permissions {a[0]:04o}\nobserved new permissions {b[0]:04o}\n"
        result.append(header + body)
    return "".join(result)


class CandidateLease:
    def __init__(self, owner, label, environment):
        self.owner, self.label, self.environment = owner, label, environment
        self.candidate_workspace = environment.workspace
        self.captured_diff = None
        self.environment._capture = self.diff

    async def diff(self):
        return await asyncio.to_thread(self._capture)

    def _capture(self):
        raw = docker("diff", self.environment.container).decode()
        paths = []
        for line in raw.splitlines():
            kind, _, path = line.partition(" ")
            if (
                not path
                or path.startswith("/tmp/.native-g22-exec-")
                or path.startswith("/tmp/opencollab-validation-")
                or path.startswith("/logs/")
            ):
                continue
            paths.append(path)
        folder = self.owner.artifacts / self.label
        before = file_snapshot(
            self.owner.source_container, paths, folder / "before-files.tar"
        )
        after = file_snapshot(
            self.environment.container, paths, folder / "after-files.tar"
        )
        diff = state_diff(before, after)
        (folder / "docker-diff.txt").write_text(raw)
        (folder / "candidate.diff").write_text(diff)
        self.captured_diff = diff
        return diff

    async def cleanup(self):
        # Native workflow session cleanup has completed before this call. The
        # task's service processes must remain available for winner grading.
        await self.environment.ensure_quiescent()


class ContainerCandidates:
    """CandidateWorkspacePort whose adoption selects an existing container."""

    def __init__(self, source_container, create_candidate, artifacts):
        self.source_container = source_container
        self.create_candidate = create_candidate
        self.artifacts = Path(artifacts)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.leases = {}
        self.selected = None

    async def acquire(self, label):
        if label in self.leases:
            raise RuntimeError("candidate label already acquired")
        env = await self.create_candidate(label)
        lease = CandidateLease(self, label, env)
        self.leases[label] = lease
        return lease

    async def source_diff(self, exclude_paths=()):
        return self.selected.captured_diff if self.selected else ""

    async def diff(self):
        return await self.source_diff()

    async def changed(self):
        return bool(await self.source_diff())

    async def changed_excluding(self, paths):
        return await self.changed()

    async def restore_source(self, patch):
        if patch:
            raise RuntimeError("original source environment must stay unchanged")
        self.selected = None

    async def adopt(self, patch, preserve_paths=()):
        raise RuntimeError("live-container adoption requires candidate identity")

    async def adopt_run(self, candidate, preserve_paths=()):
        lease = self.leases.get(candidate.label)
        if lease is None or lease.captured_diff != candidate.diff:
            raise RuntimeError(
                "candidate evidence does not identify the requested container"
            )
        await lease.environment.ensure_quiescent()
        state = await asyncio.to_thread(inspect, lease.environment.container)
        if not state["State"]["Running"]:
            raise RuntimeError("selected candidate container is not running")
        self.selected = lease
        (self.artifacts / "adoption.json").write_text(
            json.dumps(
                {
                    "label": candidate.label,
                    "container": lease.environment.container,
                    "container_id": state["Id"],
                    "candidate_bytes": len(candidate.diff.encode()),
                    "model_selected": True,
                    "adoption": "retain_original_live_container",
                },
                indent=2,
            )
            + "\n"
        )
