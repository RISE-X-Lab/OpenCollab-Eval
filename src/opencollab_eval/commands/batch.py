"""Launch, watch, pull and report one paid batch from its spec file.

    python -m opencollab_eval.commands.batch plan      experiment/batches/<name>.yaml
    python -m opencollab_eval.commands.batch preflight experiment/batches/<name>.yaml
    python -m opencollab_eval.commands.batch launch    experiment/batches/<name>.yaml [--limit 3]
    python -m opencollab_eval.commands.batch status    experiment/batches/<name>.yaml
    python -m opencollab_eval.commands.batch wait      experiment/batches/<name>.yaml
    python -m opencollab_eval.commands.batch pull      experiment/batches/<name>.yaml
    python -m opencollab_eval.commands.batch report    experiment/batches/<name>.yaml

``plan`` is local and free. ``preflight`` reads the host and refuses on any
failed check. ``launch`` runs the pre-flight, copies the instance file (data,
never source), starts the driver detached and records ``batch.json`` on both
sides. The instance file is rebuilt from the frozen suite every time, so the
task content a batch ran on is a digest in its record, not a file someone
once uploaded.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from opencollab_eval.experiment import batch_remote, cell_report
from opencollab_eval.experiment.batch_spec import (
    TEAM_ARMS,
    BatchSpec,
    HostConfig,
    SpecError,
    build_instances,
    card_file_paths,
    driver_argv,
    driver_env,
    launch_script,
    load_frame_content,
    load_host,
    load_spec,
    sha256_text,
    spec_digest,
    spec_identity,
    suite_rows,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENT_DIR = REPO_ROOT / "experiment"


class RemoteError(RuntimeError):
    pass


class Ssh:
    """The only way this tool touches the host: a script on stdin, files by scp/rsync."""

    def __init__(self, host: HostConfig) -> None:
        self.host = host

    def run(self, script: str, timeout: float = 300) -> str:
        delays = (5, 10, 15, 20, 25)
        for attempt in range(len(delays) + 1):
            proc = subprocess.run(
                ["ssh", self.host.ssh, "bash -s"],
                input=script,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if proc.returncode == 255 and attempt < len(delays):
                time.sleep(delays[attempt])
                continue
            if proc.returncode != 0:
                raise RemoteError(f"ssh {self.host.ssh} exited {proc.returncode}: {proc.stderr.strip()[:500]}")
            return proc.stdout
        raise RemoteError("unreachable")

    def copy_to(self, local_paths: Sequence[Path], remote_dir: str) -> None:
        subprocess.run(
            ["scp", "-q", *map(str, local_paths), f"{self.host.ssh}:{remote_dir}/"],
            check=True,
            timeout=600,
        )

    def pull(self, remote_dir: str, local_dir: Path) -> None:
        local_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["rsync", "-a", "--info=stats1", f"{self.host.ssh}:{remote_dir}/", f"{local_dir}/"],
            check=True,
            timeout=3600,
        )


def commit_exists(repo: str | Path, sha: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True, check=False
    )
    return proc.returncode == 0


def blob_sha256(repo: str | Path, rev: str, path: str) -> str:
    """sha256 of ``path`` at commit ``rev`` in the local checkout, without checking it out."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{path}"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SpecError(
            f"{path} does not exist at {rev[:12]} in {repo}: {proc.stderr.decode(errors='replace').strip()}"
        )
    return hashlib.sha256(proc.stdout).hexdigest()


# --- resolved batch ------------------------------------------------------------


class Batch:
    """A spec plus everything derived from it locally."""

    def __init__(self, spec_path: Path, experiment_dir: Path, host_path: Path | None = None) -> None:
        self.spec_path = spec_path
        self.spec: BatchSpec = load_spec(spec_path)
        host_file = host_path or (experiment_dir / "hosts" / f"{self.spec.host}.yaml")
        self.host: HostConfig = load_host(host_file)
        self.suite_dir = experiment_dir / "suite"
        for key, repo in (("opencollab", self.host.local_opencollab_dir), ("opencollab_eval", REPO_ROOT)):
            sha = self.spec.pins[key]
            if not commit_exists(repo, sha):
                raise SpecError(
                    f"pins.{key} {sha[:12]} is not a commit in {repo}. The host fetches from GitHub, so a pin "
                    "must be pushed: fetch it here if it exists (git fetch origin iclr-2027), or push it first."
                )
        self.rows = suite_rows(self.spec, self.suite_dir)
        self.local_dir = Path(self.host.local_batches_dir) / f"{self.spec.name}.launch"
        self.data_dir = Path(self.host.local_batches_dir) / self.spec.name
        self._instances: str | None = None

    @property
    def instances_text(self) -> str:
        if self._instances is None:
            frame = load_frame_content(self.host.frame_content)
            self._instances = build_instances(self.rows, frame)
        return self._instances

    @property
    def instances_sha(self) -> str:
        return sha256_text(self.instances_text)

    @property
    def images(self) -> list[str]:
        return [row["image"] for row in self.rows]

    @property
    def card_files(self) -> list[str]:
        return card_file_paths(self.spec, self.host.local_opencollab_dir)

    def expected_cards(self) -> dict[str, str]:
        pin = self.spec.pins["opencollab"]
        return {rel: blob_sha256(self.host.local_opencollab_dir, pin, rel) for rel in self.card_files}

    def write_inputs(self) -> Path:
        self.local_dir.mkdir(parents=True, exist_ok=True)
        path = self.local_dir / self.spec.instances_file
        path.write_text(self.instances_text, encoding="utf-8")
        return path

    def record(self, host_facts: dict[str, Any] | None = None) -> dict[str, Any]:
        suite_file = self.suite_dir / f"{self.spec.suite}.csv"
        record = {
            "spec": spec_identity(self.spec),
            "spec_digest": spec_digest(self.spec),
            "spec_file": str(self.spec_path),
            "note": self.spec.note,
            "suite_file": str(suite_file),
            "suite_sha256": hashlib.sha256(suite_file.read_bytes()).hexdigest(),
            "frame_content_sha256": hashlib.sha256(Path(self.host.frame_content).read_bytes()).hexdigest(),
            "instances": {
                "file": self.spec.instances_file,
                "sha256": self.instances_sha,
                "count": len(self.rows),
                "first": self.rows[0]["instance_id"],
                "last": self.rows[-1]["instance_id"],
            },
            "expected_card_sha256": self.expected_cards(),
            "driver_argv": driver_argv(self.spec, self.host),
            "driver_env": driver_env(self.spec, self.host),
        }
        if host_facts is not None:
            record["host"] = host_facts
        return record

    def record_path(self) -> Path:
        return self.local_dir / "batch.json"

    def previous_record(self) -> dict[str, Any] | None:
        path = self.record_path()
        if not path.exists():
            return None
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None
        return old if old.get("spec_digest") == spec_digest(self.spec) else None

    def save_record(self, record: dict[str, Any]) -> Path:
        """Write batch.json, carrying forward what this write does not know.

        `plan` runs without the host and must not erase the pre-flight's host
        facts; nothing but `launch` adds a launch, and none of them removes one.
        """
        old = self.previous_record()
        if old is not None:
            if "host" not in record and "host" in old:
                record["host"] = old["host"]
            if "launches" not in record and "launches" in old:
                record["launches"] = old["launches"]
        self.local_dir.mkdir(parents=True, exist_ok=True)
        path = self.record_path()
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        return path


# --- subcommands ---------------------------------------------------------------


def _print_checks(checks: list[batch_remote.Check]) -> bool:
    ok = True
    for check in checks:
        mark = "WARN" if check.warn else ("ok  " if check.ok else "FAIL")
        print(f"  [{mark}] {check.name}: {check.detail}")
        ok = ok and (check.ok or check.warn)
    return ok


def cmd_plan(batch: Batch, _remote: Ssh | None) -> int:
    path = batch.write_inputs()
    record = batch.record()
    batch.save_record(record)
    spec = batch.spec
    print(
        f"batch {spec.name}: arm={spec.arm} cell={spec.cell} suite={spec.suite} rows={spec.row_start}..{spec.row_stop}"
    )
    print(f"  instances: {len(batch.rows)} ({batch.rows[0]['instance_id']} .. {batch.rows[-1]['instance_id']})")
    print(f"  instance file: {path} sha256 {batch.instances_sha[:16]}")
    print(f"  pins: opencollab {spec.pins['opencollab'][:12]} opencollab_eval {spec.pins['opencollab_eval'][:12]}")
    print(f"  card files: {batch.card_files}")
    print(f"  record: {batch.record_path()}")
    print("  launch script:")
    print("    " + launch_script(spec, batch.host).replace("\n", "\n    "))
    return 0


def run_preflight(batch: Batch, remote: Ssh) -> tuple[bool, dict[str, Any]]:
    expected = batch.expected_cards()
    script = batch_remote.preflight_script(batch.spec, batch.host, list(expected), batch.images)
    facts = batch_remote.parse_facts(remote.run(script, timeout=600))
    checks = batch_remote.evaluate_preflight(batch.spec, batch.host, facts, expected, batch.instances_sha)
    print(f"pre-flight for {batch.spec.name} on {batch.host.ssh}:")
    ok = _print_checks(checks)
    return ok, batch_remote.facts_to_record(facts)


def cmd_preflight(batch: Batch, remote: Ssh) -> int:
    ok, host_facts = run_preflight(batch, remote)
    batch.save_record(batch.record(host_facts))
    print("RESULT: " + ("launchable" if ok else "NOT launchable; fix the failed checks"))
    return 0 if ok else 1


def cmd_launch(batch: Batch, remote: Ssh, limit: int | None) -> int:
    ok, host_facts = run_preflight(batch, remote)
    if not ok:
        print("RESULT: not launched")
        return 1
    instances = batch.write_inputs()
    record = batch.record(host_facts)
    old = batch.previous_record()
    record["launches"] = list((old or {}).get("launches", []))
    launched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    record["launches"].append({"at": launched_at, "limit": limit, "argv": driver_argv(batch.spec, batch.host, limit)})
    record_path = batch.save_record(record)

    remote.copy_to([instances], batch.host.workdir)
    remote.run(f"mkdir -p {shlex.quote(batch.host.workdir + '/' + batch.spec.name)}")
    remote.copy_to([record_path], f"{batch.host.workdir}/{batch.spec.name}")
    out = remote.run(launch_script(batch.spec, batch.host, limit), timeout=120)
    seen = out.strip()
    if not seen:
        print("RESULT: driver process not seen 5 s after launch; read the log:")
        print(f"  ssh {batch.host.ssh} tail -20 {batch.host.workdir}/{batch.spec.log_file}")
        return 1
    print(f"launched {batch.spec.name}" + (f" (limit {limit})" if limit else "") + f" at {launched_at}")
    print(f"  process: {seen}")
    print(f"  log: {batch.host.workdir}/{batch.spec.log_file}")
    print(f"  record: {record_path}")
    return 0


def _print_status(facts: list[tuple[str, ...]], spec: BatchSpec) -> None:
    alive = batch_remote.fact(facts, "ALIVE", "0")
    total = batch_remote.fact(facts, "TOTAL", "0")
    lines = {parts[0]: parts[1] for parts in batch_remote.facts_all(facts, "LINES") if len(parts) >= 2}
    statuses = {parts[0]: parts[1] for parts in batch_remote.facts_all(facts, "STATUS") if len(parts) >= 2}
    done = batch_remote.fact(facts, "DONE")
    if done:
        print(f"batch {spec.name}: finished (no driver alive at {done})")
    else:
        print(f"batch {spec.name}: driver {'ALIVE' if alive not in ('', '0') else 'not running'}")
    preds = lines.get(f"preds-{spec.arm}.jsonl", "0")
    print(
        f"  instances: {total}; manifest {lines.get('manifest.jsonl', '0')}, "
        f"preds {preds}, metrics {lines.get('metrics.jsonl', '0')}"
    )
    if statuses:
        print(f"  run statuses: {statuses}")
    print(f"  docker disk free: {batch_remote.fact(facts, 'DISK_FREE_GB', '?')} GB")
    for parts in batch_remote.facts_all(facts, "LOGTAIL"):
        if parts:
            print(f"  log | {parts[0]}")


def cmd_status(batch: Batch, remote: Ssh) -> int:
    facts = batch_remote.parse_facts(remote.run(batch_remote.status_script(batch.spec, batch.host), timeout=120))
    _print_status(facts, batch.spec)
    return 0


def cmd_wait(batch: Batch, remote: Ssh, poll: int, timeout: float) -> int:
    facts = batch_remote.parse_facts(
        remote.run(batch_remote.wait_script(batch.spec, batch.host, poll), timeout=timeout)
    )
    _print_status(facts, batch.spec)
    return 0


def cmd_pull(batch: Batch, remote: Ssh) -> int:
    remote.pull(f"{batch.host.workdir}/{batch.spec.name}", batch.data_dir)
    metrics = batch.data_dir / "metrics.jsonl"
    n = sum(1 for line in metrics.open(encoding="utf-8") if line.strip()) if metrics.exists() else 0
    print(f"pulled {batch.spec.name} -> {batch.data_dir} ({n} metrics rows of {len(batch.rows)} instances)")
    return 0


def cmd_report(batch: Batch, _remote: Ssh | None, scanner: str | None, json_out: Path | None) -> int:
    rows = cell_report.run_rows(batch.data_dir, batch.spec.arm)
    suite_file = batch.suite_dir / f"{batch.spec.suite}.csv"
    ordered, missing = cell_report.order_rows(rows, suite_file)
    wanted = {row["instance_id"] for row in batch.rows}
    ordered = [r for r in ordered if r.instance_id in wanted]
    missing = [i for i in missing if i in wanted]
    expected = None
    record_path = batch.record_path()
    if record_path.exists():
        try:
            expected = (
                (json.loads(record_path.read_text(encoding="utf-8")).get("host") or {}).get("role_prompt_sha256") or {}
            ).get("analyst")
        except ValueError:
            expected = None
    summary = cell_report.summarize(ordered, expected, team=batch.spec.arm in TEAM_ARMS)
    label = f"{batch.spec.arm}/{batch.spec.cell}" + (f" (rung {batch.spec.rung})" if batch.spec.rung else "")
    print(f"report for {batch.spec.name} ({label}) from {batch.data_dir}")
    pins = batch.spec.pins
    print(f"  pins: opencollab {pins['opencollab'][:12]} opencollab_eval {pins['opencollab_eval'][:12]}")
    print(cell_report.render(ordered, summary, missing))
    if json_out:
        json_out.write_text(
            json.dumps(cell_report.report_document(ordered, summary, missing), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"JSON -> {json_out}")
    scanner = None if scanner == "none" else (scanner or batch.host.scanner)
    if scanner:
        print(f"\n--- scan_batch ({scanner}) ---")
        sys.stdout.flush()
        subprocess.run([sys.executable, scanner, str(batch.data_dir)], check=False)
    return 0


# --- entry ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--experiment-dir", default=str(EXPERIMENT_DIR), help="directory holding suite/, hosts/, batches/")
    ap.add_argument("--host-config", default=None, help="override the host file named by the spec")
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("plan", "preflight", "status", "pull"):
        p = sub.add_parser(name)
        p.add_argument("spec")
    p = sub.add_parser("launch")
    p.add_argument("spec")
    p.add_argument("--limit", type=int, default=None, help="run only the first N instances (the paid pre-flight)")
    p = sub.add_parser("wait")
    p.add_argument("spec")
    p.add_argument("--poll", type=int, default=120)
    p.add_argument("--timeout", type=float, default=6 * 3600)
    p = sub.add_parser("report")
    p.add_argument("spec")
    p.add_argument(
        "--scanner", default=None, help="path to scan_batch.py; defaults to the host file's 'scanner'; 'none' skips it"
    )
    p.add_argument("--json", dest="json_out", default=None)
    return ap


def main(argv: Sequence[str] | None = None, remote_factory: Callable[[HostConfig], Any] = Ssh) -> int:
    args = build_parser().parse_args(argv)
    try:
        batch = Batch(Path(args.spec), Path(args.experiment_dir), Path(args.host_config) if args.host_config else None)
    except SpecError as exc:
        print(f"spec error: {exc}", file=sys.stderr)
        return 2
    remote = remote_factory(batch.host)
    try:
        if args.command == "plan":
            return cmd_plan(batch, None)
        if args.command == "preflight":
            return cmd_preflight(batch, remote)
        if args.command == "launch":
            return cmd_launch(batch, remote, args.limit)
        if args.command == "status":
            return cmd_status(batch, remote)
        if args.command == "wait":
            return cmd_wait(batch, remote, args.poll, args.timeout)
        if args.command == "pull":
            return cmd_pull(batch, remote)
        if args.command == "report":
            return cmd_report(batch, None, args.scanner, Path(args.json_out) if args.json_out else None)
    except (SpecError, RemoteError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
