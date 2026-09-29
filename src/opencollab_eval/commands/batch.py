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
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from opencollab_eval.commands import batch_state
from opencollab_eval.commands.batch_reporting import (
    _cell_rows as _cell_rows,
)
from opencollab_eval.commands.batch_reporting import (
    batch_records as batch_records,
)
from opencollab_eval.commands.batch_reporting import (
    cmd_report as cmd_report,
)
from opencollab_eval.commands.batch_reporting import (
    replacement_batches as replacement_batches,
)
from opencollab_eval.commands.batch_reporting import (
    retry_batches as retry_batches,
)
from opencollab_eval.commands.batch_scoring import (
    _score_facts as _score_facts,
)
from opencollab_eval.commands.batch_scoring import (
    cmd_score as cmd_score,
)
from opencollab_eval.commands.batch_scoring import (
    cmd_score_report as cmd_score_report,
)
from opencollab_eval.engine.async_runtime import add_exception_note
from opencollab_eval.engine.swe_eval_records import MAX_JSON_DOCUMENT_BYTES, MAX_JSONL_SCAN_BYTES
from opencollab_eval.experiment import batch_remote
from opencollab_eval.experiment.batch_spec import (
    BatchSpec,
    HostConfig,
    SpecError,
    build_instances,
    card_file_paths,
    cell_team_file,
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
from opencollab_eval.safe_files import read_regular_bytes, read_regular_text, write_regular_bytes_atomic

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENT_DIR = REPO_ROOT / "experiment"

#: The fields a retry batch may differ from the batch it retries in, and
#: nothing else. ``name`` and ``rows`` are what make it a second attempt at
#: part of the same slice; ``concurrency`` is already outside the digest
#: (a resume may use a different one); ``note`` and ``retry_of`` are prose and
#: the pointer itself. Every other field is what the batch paid for, and a
#: retry that changed one would be a different cell reported as the same one.
#: ``replaces`` is in the list because a retry of a replacement batch is a
#: retry and not a second replacement: it must not be found again by the
#: replacement merge, so it carries no ``replaces`` of its own. A spec that set
#: both is refused at load time.
RETRY_MAY_DIFFER = frozenset({"name", "rows", "retry_of", "replaces", "note", "concurrency"})

#: The host-file fields in which two checkouts of one machine differ (``lthpc``
#: and ``lthpc-b``): which directory holds the code. A retry may name the other
#: checkout -- that is what the second one is for -- because the pins are still
#: compared, so the commit that runs is the one the original ran; any other
#: host field (workdir, python, scoring dataset) would be another instrument.
CHECKOUT_ONLY_FIELDS = frozenset({"name", "opencollab_dir", "eval_dir"})


def same_machine(a: HostConfig, b: HostConfig) -> bool:
    """Two host files that differ at most in which checkout they use."""
    fa, fb = vars(a), vars(b)
    return all(fa[key] == fb[key] for key in fa if key not in CHECKOUT_ONLY_FIELDS)


#: The same list for a replacement, plus ``suite``. A replacement runs an
#: instance the cell never drew, and the reserve row it comes from need not
#: live in the file the cell was sliced out of -- the ordered draw is longer
#: than the suite, and the subset is a draw of its own. Everything else is
#: still what the batch paid for: a replacement at another budget or another
#: card would be a different cell entering the same numbers.
REPLACES_MAY_DIFFER = frozenset({"name", "suite", "rows", "replaces", "note", "concurrency"})


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


def blob_text(repo: str | Path, rev: str, path: str) -> str:
    """``path`` at commit ``rev`` in the local checkout, without checking it out."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{path}"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SpecError(
            f"{path} does not exist at {rev[:12]} in {repo}: {proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.stdout.decode("utf-8")


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


def original_slice(spec: BatchSpec, record_spec: dict[str, Any]) -> BatchSpec:
    """``spec`` re-pointed at the suite slice a recorded batch ran.

    Both the retry check and the replacement check ask "which instances did
    that batch run", and a replacement may cite a different suite file than the
    batch it stands in for, so the suite is taken from the record too.
    """
    rows = record_spec.get("rows") or {}
    return replace(
        spec,
        suite=str(record_spec.get("suite") or spec.suite),
        row_start=rows.get("start"),
        row_stop=rows.get("stop"),
    )


class Batch:
    """A spec plus everything derived from it locally."""

    def __init__(self, spec_path: Path, experiment_dir: Path, host_path: Path | None = None) -> None:
        self.spec_path = spec_path
        self.spec: BatchSpec = load_spec(spec_path)
        host_file = host_path or (experiment_dir / "hosts" / f"{self.spec.host}.yaml")
        self.host: HostConfig = load_host(host_file)
        self.hosts_dir = Path(host_file).parent
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
        self.check_retry()
        self.check_replaces()

    def check_retry(self) -> None:
        """Refuse a ``retry_of`` that is not a second attempt at the same cell.

        The whole point of the field is that two out-dirs are reported as one
        cell. That is a claim about the instrument, so it is checked rather
        than trusted: the batch named must have been planned, every paid field
        must be the one it paid, and these rows must be rows it ran. Without
        the check, a retry at a different budget or a different card would be
        merged into the original's numbers with nothing saying so.
        """
        name = self.spec.retry_of
        if name is None:
            return
        record_path = Path(self.host.local_batches_dir) / f"{name}.launch" / "batch.json"
        if not record_path.exists():
            raise SpecError(
                f"retry_of {name!r}: {record_path} does not exist. A retry is merged into that batch's "
                "report, so the batch has to have been planned here first."
            )
        try:
            record = json.loads(read_regular_text(record_path, max_bytes=MAX_JSON_DOCUMENT_BYTES))
        except ValueError as exc:
            raise SpecError(f"retry_of {name!r}: {record_path} is not readable JSON ({exc})") from exc
        theirs = record.get("spec") or {}
        mine = spec_identity(self.spec)
        differ = sorted(
            key for key in set(mine) | set(theirs) if key not in RETRY_MAY_DIFFER and mine.get(key) != theirs.get(key)
        )
        if "host" in differ:
            other = self.hosts_dir / f"{theirs.get('host')}.yaml"
            if other.exists() and same_machine(self.host, load_host(other)):
                differ.remove("host")
        if differ:
            detail = "; ".join(f"{key}: this {mine.get(key)!r} vs {name} {theirs.get(key)!r}" for key in differ)
            raise SpecError(f"retry_of {name!r}: a retry may differ only in {sorted(RETRY_MAY_DIFFER)}, but {detail}")
        ran = {row["instance_id"] for row in suite_rows(original_slice(self.spec, theirs), self.suite_dir)}
        outside = [row["instance_id"] for row in self.rows if row["instance_id"] not in ran]
        if outside:
            raise SpecError(
                f"retry_of {name!r}: rows {self.spec.row_start}..{self.spec.row_stop} include "
                f"{outside} which that batch never ran; a retry can only re-attempt its own instances."
            )

    def check_replaces(self) -> None:
        """Refuse a ``replaces`` that is not one reserve row standing in for one drawn row.

        A replacement is the only edit that changes *which tasks* a cell
        reports, so each half of the claim is checked rather than trusted. The
        instance leaving has to be one this batch actually ran; the instance
        arriving has to be one it did not, or the cell would report the same
        task twice; and there has to be exactly one of it, because a slice here
        would quietly regrow the cell.
        """
        if self.spec.replaces is None:
            return
        name = self.spec.replaces["batch"]
        instance = self.spec.replaces["instance"]
        record_path = Path(self.host.local_batches_dir) / f"{name}.launch" / "batch.json"
        if not record_path.exists():
            raise SpecError(
                f"replaces {name!r}: {record_path} does not exist. A replacement is merged into that "
                "batch's report, so the batch has to have been planned here first."
            )
        try:
            record = json.loads(read_regular_text(record_path, max_bytes=MAX_JSON_DOCUMENT_BYTES))
        except ValueError as exc:
            raise SpecError(f"replaces {name!r}: {record_path} is not readable JSON ({exc})") from exc
        theirs = record.get("spec") or {}
        mine = spec_identity(self.spec)
        differ = sorted(
            key
            for key in set(mine) | set(theirs)
            if key not in REPLACES_MAY_DIFFER and mine.get(key) != theirs.get(key)
        )
        if differ:
            detail = "; ".join(f"{key}: this {mine.get(key)!r} vs {name} {theirs.get(key)!r}" for key in differ)
            raise SpecError(
                f"replaces {name!r}: a replacement may differ only in {sorted(REPLACES_MAY_DIFFER)}, but {detail}"
            )
        if len(self.rows) != 1:
            raise SpecError(
                f"replaces {name!r}: a replacement runs exactly one instance, but rows "
                f"{self.spec.row_start}..{self.spec.row_stop} of {self.spec.suite} select "
                f"{len(self.rows)} ({[row['instance_id'] for row in self.rows]})"
            )
        ran = {row["instance_id"] for row in suite_rows(original_slice(self.spec, theirs), self.suite_dir)}
        if instance not in ran:
            raise SpecError(
                f"replaces {name!r}: {instance} is not an instance that batch ran, so replacing it "
                f"would take nothing out of its report."
            )
        mine_instance = self.rows[0]["instance_id"]
        if mine_instance in ran:
            raise SpecError(
                f"replaces {name!r}: {mine_instance} is already in that batch's slice. A second run of an "
                "instance the cell has is a retry (retry_of), not a replacement."
            )
        for _, other_name, other_spec in batch_records(Path(self.host.local_batches_dir)):
            if other_name == self.spec.name:
                continue
            other_replaces = other_spec.get("replaces") or {}
            if other_replaces.get("batch") != name:
                continue
            if other_replaces.get("instance") == instance:
                raise SpecError(f"replaces {name!r}: {instance} is already replaced by planned batch {other_name!r}")
            other_rows = suite_rows(original_slice(self.spec, other_spec), self.suite_dir)
            if len(other_rows) != 1:
                raise SpecError(f"replaces {name!r}: planned batch {other_name!r} has no single replacement instance")
            if other_rows[0]["instance_id"] == mine_instance:
                raise SpecError(
                    f"replaces {name!r}: {mine_instance} is already used as a replacement by {other_name!r}"
                )

    @property
    def frame_content(self) -> str:
        """The frame content this batch's rows come from: the spec's, else the host's."""
        return self.spec.frame_content or self.host.frame_content

    @property
    def instances_text(self) -> str:
        if self._instances is None:
            frame = load_frame_content(self.frame_content)
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

    def declared_profiles(self) -> dict[str, str]:
        """The agent profile each seat of this cell declares, at the pin.

        A card digest says which words a seat was given; it cannot say which
        agent read them. A seat declaring ``profile: single2`` runs that
        profile's system prompt, history shaper, safety wrapper and tool output
        caps with the card appended -- so two cells carrying the same card
        under different profiles are two conditions, and a record holding only
        the digests cannot tell them apart. Read from the pinned team file, the
        same bytes pre-flight compares on the host.

        ``"default"`` is OpenCollab's own agent, which is what a seat that
        declares nothing runs as.
        """
        if self.spec.cell is None:
            return {}
        pin = self.spec.pins["opencollab"]
        rel = cell_team_file(self.spec.cell)
        raw = yaml.safe_load(blob_text(self.host.local_opencollab_dir, pin, rel)) or {}
        roles = raw.get("roles") or {}
        return {role: str((entry or {}).get("profile") or "default") for role, entry in roles.items()}

    def write_inputs(self) -> Path:
        path = self.local_dir / self.spec.instances_file
        write_regular_bytes_atomic(path, self.instances_text.encode("utf-8"))
        return path

    def record(self, host_facts: dict[str, Any] | None = None) -> dict[str, Any]:
        suite_file = self.suite_dir / f"{self.spec.suite}.csv"
        record = {
            "spec": spec_identity(self.spec),
            "spec_digest": spec_digest(self.spec),
            "spec_file": str(self.spec_path),
            "note": self.spec.note,
            "suite_file": str(suite_file),
            "suite_sha256": hashlib.sha256(read_regular_bytes(suite_file, max_bytes=MAX_JSONL_SCAN_BYTES)).hexdigest(),
            "frame_content_sha256": hashlib.sha256(
                read_regular_bytes(Path(self.frame_content), max_bytes=MAX_JSONL_SCAN_BYTES)
            ).hexdigest(),
            "instances": {
                "file": self.spec.instances_file,
                "sha256": self.instances_sha,
                "count": len(self.rows),
                "first": self.rows[0]["instance_id"],
                "last": self.rows[-1]["instance_id"],
            },
            "expected_card_sha256": self.expected_cards(),
            "declared_role_profiles": self.declared_profiles(),
            "driver_argv": driver_argv(self.spec, self.host),
            "driver_env": driver_env(self.spec, self.host),
        }
        if host_facts is not None:
            record["host"] = host_facts
        return record

    def record_path(self) -> Path:
        return self.local_dir / "batch.json"

    def previous_record(self, record: dict[str, Any] | None = None) -> dict[str, Any] | None:
        return batch_state.previous_record(self, record)

    def save_record(self, record: dict[str, Any]) -> Path:
        return batch_state.save_record(self, record)


# --- subcommands ---------------------------------------------------------------


def _print_checks(checks: list[batch_remote.Check]) -> bool:
    ok = True
    for check in checks:
        mark = "WARN" if check.warn else ("ok  " if check.ok else "FAIL")
        print(f"  [{mark}] {check.name}: {check.detail}")
        ok = ok and (check.ok or check.warn)
    return ok


def cmd_plan(batch: Batch, _remote: Ssh | None) -> int:
    with batch_state.record_transaction(batch):
        record = batch.record()
        batch.previous_record(record)
        path = batch.write_inputs()
        batch.save_record(record)
    spec = batch.spec
    print(
        f"batch {spec.name}: arm={spec.arm} cell={spec.cell} suite={spec.suite} rows={spec.row_start}..{spec.row_stop}"
        + (f" (retry of {spec.retry_of})" if spec.retry_of else "")
        + (f" (replaces {spec.replaces['instance']} of {spec.replaces['batch']})" if spec.replaces else "")
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
    # A second script makes the authenticated request. Only non-secret
    # configuration fields from the first script enter batch.json.
    probe = batch_remote.endpoint_probe_script(batch.host, batch.spec.model_env, batch.spec.env)
    facts += batch_remote.parse_facts(remote.run(probe, timeout=180))
    checks = batch_remote.evaluate_preflight(batch.spec, batch.host, facts, expected, batch.instances_sha)
    print(f"pre-flight for {batch.spec.name} on {batch.host.ssh}:")
    ok = _print_checks(checks)
    return ok, batch_remote.facts_to_record(facts)


def cmd_sync(batch: Batch, remote: Ssh) -> int:
    """Bring the host's two checkouts to the spec's pins, or refuse and say why.

    Pre-flight reports a pin mismatch and stops there, so every batch so far has
    been preceded by a hand-run fetch and checkout over ssh -- the step where
    the wrong repository, the wrong sha, or a tree somebody left dirty does not
    announce itself. Two guards run before anything is written: a live driver
    (the checkout would move the code under a running batch) and local
    modifications (the pin would stop naming what ran).
    """
    facts = batch_remote.parse_facts(remote.run(batch_remote.sync_guard_script(batch.host)))
    # A fact line can carry tabs of its own (a process command line does), so
    # these read by index rather than unpacking a pair.
    running = [f[1] for f in facts if f[0] == "RUNNING" and len(f) > 1]
    elsewhere = [f[1] for f in facts if f[0] == "ELSEWHERE" and len(f) > 1]
    decoy = next((f[1] for f in facts if f[0] == "DECOY_HIT" and len(f) > 1), "0")
    before = {f[0]: f[1] for f in facts if f[0].endswith("_BEFORE") and len(f) > 1}
    dirty = {f[0]: f[1] for f in facts if f[0].endswith("_DIRTY") and len(f) > 1}
    want = {"OC": batch.spec.pins.get("opencollab", ""), "EV": batch.spec.pins.get("opencollab_eval", "")}

    print(f"sync {batch.spec.name} on {batch.host.ssh}:")
    for tag, name in (("OC", batch.host.opencollab_dir), ("EV", batch.host.eval_dir)):
        print(f"  {name}: host {before.get(tag + '_BEFORE', '?')[:12]} -> spec {want[tag][:12] or '(none)'}")
    if decoy == "0":
        print("  REFUSED: the running-batch check found not even its own decoy, so a quiet")
        print("           result from it means nothing. Nothing was changed.")
        return 1
    if elsewhere:
        print(f"  {len(elsewhere)} driver(s) on other checkouts of this machine; they do not use this tree")
    if running:
        print(f"  REFUSED: {len(running)} driver(s) alive on this host; a checkout would move")
        print("           the code under a running batch. Nothing was changed.")
        for line in running:
            print(f"           {line}")
        return 1
    unclean = [tag for tag in ("OC", "EV") if dirty.get(tag + "_DIRTY", "0") not in {"0", ""}]
    if unclean:
        print(f"  REFUSED: {', '.join(unclean)} has modified tracked files. Nothing was changed.")
        return 1
    if not all(want.values()):
        print("  REFUSED: the spec does not pin both repositories. Nothing was changed.")
        return 1

    script = batch_remote.sync_script(batch.host, want["OC"], want["EV"])
    out = batch_remote.parse_facts(remote.run(script, timeout=900))
    after = {f[0]: f[1] for f in out if f[0].endswith("_AFTER") and len(f) > 1}
    proxies = {f[0]: f[1] for f in out if f[0].endswith("_PROXY") and len(f) > 1}
    for fact in out:
        if fact[0].endswith("_FETCH_RETRY") and len(fact) > 1:
            tag = fact[0].split("_")[0]
            # A retry is where a fetch that cannot reach GitHub first becomes
            # visible, and the commonest reason on these hosts is that the
            # proxy resolved to nothing. Say which one it used, here, rather
            # than leaving the reader to find it by hand afterwards.
            print(f"  {tag}: fetch retry {fact[1]} (proxy {proxies.get(tag + '_PROXY', 'unknown')})")
    problems = [" ".join(f) for f in out if f[0].endswith("_MISSING") or f[0].endswith("_CHECKOUT_FAILED")]
    ok = True
    for tag, name in (("OC", batch.host.opencollab_dir), ("EV", batch.host.eval_dir)):
        got = after.get(tag + "_AFTER", "")
        hit = got == want[tag]
        ok = ok and hit
        print(f"  [{'ok  ' if hit else 'FAIL'}] {name} now {got[:12] or '(no answer)'}")
    for problem in problems:
        print(f"  {problem}")
    print("RESULT: " + ("synced" if ok else "NOT synced"))
    return 0 if ok else 1


def cmd_go(batch: Batch, remote: Ssh, limit: int | None) -> int:
    """sync, then launch. One command for the whole paid path.

    Launch runs pre-flight itself, so the checks are not skipped by going
    through here -- what is removed is the hand-run ssh between them.
    """
    rc = cmd_sync(batch, remote)
    if rc != 0:
        print("RESULT: not launched")
        return rc
    return cmd_launch(batch, remote, limit)


def cmd_preflight(batch: Batch, remote: Ssh) -> int:
    with batch_state.launch_transaction(batch):
        ok, host_facts = run_preflight(batch, remote)
        with batch_state.record_transaction(batch, host_facts):
            batch.save_record(batch.record(host_facts))
    print("RESULT: " + ("launchable" if ok else "NOT launchable; fix the failed checks"))
    return 0 if ok else 1


def cmd_launch(batch: Batch, remote: Ssh, limit: int | None) -> int:
    with batch_state.launch_transaction(batch):
        return _launch_locked(batch, remote, limit)


def _launch_locked(batch: Batch, remote: Ssh, limit: int | None) -> int:
    if batch.spec.derived:
        # The out-dir a derived spec names holds predictions assembled from a
        # finished batch, not runs. Everything downstream -- score, report --
        # treats it like any other batch, which is the point; launching into it
        # would spend money to overwrite the thing being read.
        print(f"REFUSED: {batch.spec.name} is a derived spec (derived: true); it addresses")
        print("         predictions built from data already collected. Nothing was launched.")
        return 1
    ok, host_facts = run_preflight(batch, remote)
    if not ok:
        print("RESULT: not launched")
        return 1
    script = launch_script(batch.spec, batch.host, limit)
    with batch_state.record_transaction(batch, host_facts):
        record = batch.record(host_facts)
        old = batch.previous_record(record)
        instances = batch.write_inputs()
        record["launches"] = list((old or {}).get("launches", []))
        launched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        launch_index = len(record["launches"])
        record["launches"].append(
            {
                "at": launched_at,
                "limit": limit,
                "argv": driver_argv(batch.spec, batch.host, limit),
                "model_identity": batch_state.model_identity(host_facts),
                "state": batch_state.PREPARING,
            }
        )
        record_path = batch.save_record(record)

    try:
        remote.copy_to([instances], batch.host.workdir)
        remote.run(f"mkdir -p {shlex.quote(batch.host.workdir + '/' + batch.spec.name)}")
        # Write uncertainty before the last upload and dispatch, so an
        # interrupted caller leaves a conservative model claim.
        batch_state.set_launch_state(batch, launch_index, batch_state.START_UNKNOWN)
        remote.copy_to([record_path], f"{batch.host.workdir}/{batch.spec.name}")
    except BaseException as exc:
        try:
            batch_state.set_launch_state(batch, launch_index, batch_state.NOT_STARTED)
        except BaseException as state_exc:
            add_exception_note(exc, f"could not record confirmed non-start for {batch.spec.name}: {state_exc}")
        raise

    out = remote.run(script, timeout=120)
    seen = out.strip()
    if not seen:
        print("RESULT: driver process not seen 5 s after launch; read the log:")
        print(f"  ssh {batch.host.ssh} tail -20 {batch.host.workdir}/{batch.spec.log_file}")
        return 1
    batch_state.set_launch_state(batch, launch_index, batch_state.STARTED)
    try:
        remote.copy_to([record_path], f"{batch.host.workdir}/{batch.spec.name}")
    except Exception as exc:
        print(f"warning: launched driver, but remote batch record update failed: {exc}", file=sys.stderr)
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


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--experiment-dir", default=str(EXPERIMENT_DIR), help="directory holding suite/, hosts/, batches/")
    ap.add_argument("--host-config", default=None, help="override the host file named by the spec")
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("plan", "preflight", "status", "pull", "sync"):
        p = sub.add_parser(name)
        p.add_argument("spec")
    p = sub.add_parser("go")
    p.add_argument("spec")
    p.add_argument("--limit", type=int, default=None, help="run only the first N instances (the paid pre-flight)")
    p = sub.add_parser("launch")
    p.add_argument("spec")
    p.add_argument("--limit", type=int, default=None, help="run only the first N instances (the paid pre-flight)")
    p = sub.add_parser("wait")
    p.add_argument("spec")
    p.add_argument("--poll", type=int, default=120)
    p.add_argument("--timeout", type=float, default=6 * 3600)
    p = sub.add_parser("score-report")
    p.add_argument("spec")
    p = sub.add_parser("score")
    p.add_argument("spec")
    p.add_argument("--max-workers", type=int, default=12)
    p.add_argument("--timeout", type=int, default=1800, help="per-instance seconds inside the harness")
    p.add_argument(
        "--no-gold",
        dest="gold",
        action="store_false",
        help="skip the reference-patch control; only for a host whose control already passed today",
    )
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
        if args.command == "sync":
            return cmd_sync(batch, remote)
        if args.command == "go":
            return cmd_go(batch, remote, args.limit)
        if args.command == "status":
            return cmd_status(batch, remote)
        if args.command == "wait":
            return cmd_wait(batch, remote, args.poll, args.timeout)
        if args.command == "pull":
            return cmd_pull(batch, remote)
        if args.command == "score":
            return cmd_score(batch, remote, max_workers=args.max_workers, timeout=args.timeout, gold=args.gold)
        if args.command == "score-report":
            return cmd_score_report(batch, remote)
        if args.command == "report":
            return cmd_report(batch, None, args.scanner, Path(args.json_out) if args.json_out else None)
    except (SpecError, RemoteError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        for note in getattr(exc, "__notes__", ()):
            print(f"  {note}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
