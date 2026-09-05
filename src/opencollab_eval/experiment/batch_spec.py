"""One paid batch, written down before it is launched.

Every batch this experiment has run so far was launched by hand: an agent read
the driver's source, rebuilt the instance file from the benchmark, looked up the
three environment switches in a shell script on the machine, checked out two
commits, and typed a twelve-line command. Each of those steps was rediscovered
each time, and two of them (the shadowed import path and the unquoted ``off``)
fail silently when missed. This module is the written-down version: a spec
file names the cell, the frozen task list, the budget, the environment and the
two commits; the functions here turn it into the driver's exact command line,
the instance file byte for byte, and the pre-flight checks that refuse to
launch when the machine does not match the spec.

Nothing here is a default. A paid setting that is not in the spec is an error,
because a default that drifted looks exactly like one that did not.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from opencollab_eval.generation.gen_prediction_batch import ARM_MODULES, TEAM_ARMS

DRIVER_MODULE = "opencollab_eval.generation.gen_prediction_batch"

# The three switches that decide what a run records and what the model is told.
# They are the batch's version identity next to the two commits, so a spec that
# omits one is rejected rather than inheriting whatever the shell had.
REQUIRED_ENV = (
    "OPENCOLLAB_LLM_STREAM_CHAT",
    "OPENCOLLAB_REASONING_EFFORT",
    "OPENCOLLAB_WRITE_NUDGE_MODE",
)

# The paper's rung names against the team files that seat them (§5, D18). A
# spec may name the rung, the cell, or both; both that disagree is an error,
# because a rung's number must come from the card the paper says it comes from.
RUNG_CELLS = {
    "primary": "facts-v2",
    "opt-out": "cmd-optout",
    "bare": "cmd-bare",
    "plain": "cmd-plain",
    "prohibit": "cmd-prohibit",
}

# ``pgrep -f`` matches its own caller's command line, so the pattern carries a
# bracket that the literal string in a shell command does not match.
BATCH_PROCESS_PATTERN = "[o]pencollab_eval.generation.gen_prediction_batch"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class SpecError(ValueError):
    """The spec or host file cannot be executed as written."""


@dataclass(frozen=True)
class HostConfig:
    """Where and how batches run on one machine. Facts, not choices."""

    name: str
    ssh: str
    workdir: str
    python: str
    opencollab_dir: str
    eval_dir: str
    proxy: str | None
    docker_disk: str
    min_free_gb: float
    local_batches_dir: str
    local_opencollab_dir: str
    frame_content: str
    scanner: str | None = None

    @property
    def pythonpath(self) -> str:
        return f"{self.workdir}/{self.opencollab_dir}:{self.workdir}/{self.eval_dir}/src"


@dataclass(frozen=True)
class BatchSpec:
    """One out-dir's worth of runs: one arm, one cell, one ordered task list."""

    name: str
    host: str
    arm: str
    cell: str | None
    rung: str | None
    suite: str
    row_start: int | None
    row_stop: int | None
    budget_per_seat: int
    max_steps: int
    timeout: float
    concurrency: int
    model_env: str
    env: dict[str, str]
    pins: dict[str, str]
    #: The batch this one re-attempts. A run the endpoint dropped leaves a
    #: prediction row behind, so a resume of the original skips it; the second
    #: attempt has to be its own out-dir, and this field is what says the two
    #: directories are one cell rather than two. ``plan`` refuses it unless the
    #: named batch was planned, every paid field matches, and these rows are a
    #: subset of that batch's rows -- otherwise "same cell" would be a claim
    #: nothing checked.
    retry_of: str | None = None
    #: The pre-registered instance this batch stands in for, and the batch that
    #: ran it: ``{"batch": <name>, "instance": <id>}``. Used when an
    #: instance's evaluation environment is unusable -- the benchmark's own gold
    #: patch does not resolve there, so no run of any arm on it can be scored.
    #: That is not a random event, so the replacement is the next row of the
    #: ordered draw rather than a choice made after the fact. ``plan`` refuses
    #: it unless the named batch was planned, every paid field matches, this
    #: spec is one row, the named instance is one that batch ran, and this
    #: spec's own instance is not.
    replaces: dict[str, str] | None = None
    note: str = ""
    source: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def instances_file(self) -> str:
        return f"{self.name}-instances.jsonl"

    @property
    def log_file(self) -> str:
        return f"{self.name}.log"

    def team_config_relpath(self, host: HostConfig) -> str | None:
        if self.cell is None:
            return None
        return f"{host.opencollab_dir}/configs/team.handoff.{self.cell}.yaml"


def _require(mapping: dict[str, Any], key: str, kind: type, where: str) -> Any:
    if key not in mapping:
        raise SpecError(f"{where}: missing required field '{key}'")
    value = mapping[key]
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
        raise SpecError(f"{where}: field '{key}' must be {kind.__name__}, got {type(value).__name__}")
    return value


def load_host(path: str | Path) -> HostConfig:
    where = str(path)
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SpecError(f"{where}: not a mapping")
    return HostConfig(
        name=_require(raw, "name", str, where),
        ssh=_require(raw, "ssh", str, where),
        workdir=_require(raw, "workdir", str, where).rstrip("/"),
        python=_require(raw, "python", str, where),
        opencollab_dir=_require(raw, "opencollab_dir", str, where).strip("/"),
        eval_dir=_require(raw, "eval_dir", str, where).strip("/"),
        proxy=raw.get("proxy"),
        docker_disk=_require(raw, "docker_disk", str, where),
        min_free_gb=_require(raw, "min_free_gb", float, where),
        local_batches_dir=str(Path(_require(raw, "local_batches_dir", str, where)).expanduser()),
        local_opencollab_dir=str(Path(_require(raw, "local_opencollab_dir", str, where)).expanduser()),
        frame_content=str(Path(_require(raw, "frame_content", str, where)).expanduser()),
        scanner=(str(Path(raw["scanner"]).expanduser()) if raw.get("scanner") else None),
    )


def load_spec(path: str | Path) -> BatchSpec:
    where = str(path)
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SpecError(f"{where}: not a mapping")

    name = _require(raw, "name", str, where)
    if not _NAME.match(name):
        raise SpecError(f"{where}: name {name!r} is not a safe directory name")
    arm = _require(raw, "arm", str, where)
    if arm not in ARM_MODULES:
        raise SpecError(f"{where}: arm {arm!r} is not one of {sorted(ARM_MODULES)}")
    cell = raw.get("cell")
    rung = raw.get("rung")
    if rung is not None:
        if rung not in RUNG_CELLS:
            raise SpecError(f"{where}: rung {rung!r} is not one of {list(RUNG_CELLS)}")
        if arm not in TEAM_ARMS:
            raise SpecError(f"{where}: a rung is a Team card; arm {arm!r} has none")
        if cell is None:
            cell = RUNG_CELLS[rung]
        elif cell != RUNG_CELLS[rung]:
            raise SpecError(
                f"{where}: rung {rung!r} is the card {RUNG_CELLS[rung]!r}, but cell says {cell!r}; "
                "a number reported under a rung name must come from that rung's card"
            )
    if arm in TEAM_ARMS and not isinstance(cell, str):
        raise SpecError(f"{where}: arm {arm!r} needs a 'cell' (the team.handoff.<cell>.yaml to seat) or a 'rung'")
    if arm not in TEAM_ARMS and cell is not None:
        raise SpecError(f"{where}: arm {arm!r} takes no 'cell'; the driver would ignore it silently")

    rows = raw.get("rows") or {}
    if not isinstance(rows, dict):
        raise SpecError(f"{where}: 'rows' must be a mapping with 'start' and/or 'stop'")
    row_start = rows.get("start")
    row_stop = rows.get("stop")
    for label, value in (("start", row_start), ("stop", row_stop)):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
            raise SpecError(f"{where}: rows.{label} must be a positive integer (1-based, inclusive)")

    env_raw = raw.get("env")
    if not isinstance(env_raw, dict):
        raise SpecError(f"{where}: 'env' must be a mapping and must set {', '.join(REQUIRED_ENV)}")
    env: dict[str, str] = {}
    for key, value in env_raw.items():
        if isinstance(value, bool):
            # YAML reads ``off``, ``on``, ``true``, ``no`` as booleans. The
            # runtime reads strings, and ``False`` is not ``off``.
            raise SpecError(f'{where}: env.{key} was parsed as a boolean; quote the value ("{str(value).lower()}")')
        if not isinstance(value, (str, int, float)):
            raise SpecError(f"{where}: env.{key} must be a scalar")
        env[str(key)] = str(value)
    missing = [key for key in REQUIRED_ENV if key not in env]
    if missing:
        raise SpecError(f"{where}: env is missing {', '.join(missing)}")

    retry_of = raw.get("retry_of")
    if retry_of is not None:
        if not isinstance(retry_of, str) or not _NAME.match(retry_of):
            raise SpecError(f"{where}: retry_of must be the name of another batch")
        if retry_of == name:
            raise SpecError(f"{where}: retry_of {retry_of!r} is this batch's own name; a retry needs its own out-dir")

    replaces = raw.get("replaces")
    if replaces is not None:
        if not isinstance(replaces, dict) or set(replaces) != {"batch", "instance"}:
            raise SpecError(
                f"{where}: replaces must be a mapping with exactly 'batch' and 'instance'; got {replaces!r}"
            )
        if not isinstance(replaces["batch"], str) or not _NAME.match(replaces["batch"]):
            raise SpecError(f"{where}: replaces.batch must be the name of another batch")
        if not isinstance(replaces["instance"], str) or not replaces["instance"].strip():
            raise SpecError(f"{where}: replaces.instance must be an instance id")
        if replaces["batch"] == name:
            raise SpecError(
                f"{where}: replaces.batch {name!r} is this batch's own name; a replacement needs its own out-dir"
            )
        if retry_of is not None:
            raise SpecError(
                f"{where}: a spec is either a second attempt at an instance the cell keeps (retry_of) or a "
                "stand-in for one it drops (replaces), never both"
            )
        replaces = {"batch": replaces["batch"], "instance": replaces["instance"]}

    pins_raw = _require(raw, "pins", dict, where)
    pins: dict[str, str] = {}
    for key in ("opencollab", "opencollab_eval"):
        value = pins_raw.get(key)
        if not isinstance(value, str) or not _SHA.match(value):
            raise SpecError(f"{where}: pins.{key} must be a full 40-character commit sha")
        pins[key] = value

    spec = BatchSpec(
        name=name,
        host=_require(raw, "host", str, where),
        arm=arm,
        cell=cell,
        rung=rung,
        suite=_require(raw, "suite", str, where),
        row_start=row_start,
        row_stop=row_stop,
        budget_per_seat=_require(raw, "budget_per_seat", int, where),
        max_steps=_require(raw, "max_steps", int, where),
        timeout=_require(raw, "timeout", float, where),
        concurrency=_require(raw, "concurrency", int, where),
        model_env=_require(raw, "model_env", str, where),
        env=env,
        pins=pins,
        retry_of=retry_of,
        replaces=replaces,
        note=str(raw.get("note") or ""),
        source=raw,
    )
    if spec.budget_per_seat <= 0 or spec.max_steps <= 0 or spec.timeout <= 0 or spec.concurrency <= 0:
        raise SpecError(f"{where}: budget_per_seat, max_steps, timeout and concurrency must be positive")
    return spec


def spec_identity(spec: BatchSpec) -> dict[str, Any]:
    """The fields that make two launches the same batch (resumable into one out-dir)."""
    identity: dict[str, Any] = {
        "name": spec.name,
        "host": spec.host,
        "arm": spec.arm,
        "cell": spec.cell,
        "rung": spec.rung,
        "suite": spec.suite,
        "rows": {"start": spec.row_start, "stop": spec.row_stop},
        "budget_per_seat": spec.budget_per_seat,
        "max_steps": spec.max_steps,
        "timeout": spec.timeout,
        "concurrency": spec.concurrency,
        "model_env": spec.model_env,
        "env": dict(sorted(spec.env.items())),
        "pins": dict(sorted(spec.pins.items())),
    }
    if spec.retry_of is not None:
        # Present only when it is set, and for a reason that is not tidiness:
        # this dict is the digest, and the digest is how the pre-flight decides
        # whether an out-dir holds *this* batch. A key added unconditionally
        # would have changed the digest of every spec already launched, so
        # every finished batch would have read as a different batch on resume.
        identity["retry_of"] = spec.retry_of
    if spec.replaces is not None:
        # Written into the identity for the same two reasons ``retry_of`` is,
        # and only when set for the same one: a replacement must never be
        # resumed into the batch it replaces an instance of, and a key added
        # unconditionally would move the digest of every spec already launched.
        identity["replaces"] = dict(sorted(spec.replaces.items()))
    return identity


def spec_digest(spec: BatchSpec) -> str:
    identity = spec_identity(spec)
    identity.pop("concurrency")  # a resume may use a different concurrency; it is not a treatment
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()


# --- task list ---------------------------------------------------------------


def suite_rows(spec: BatchSpec, suite_dir: str | Path) -> list[dict[str, str]]:
    """The ordered rows this batch runs, sliced by the frozen ``order`` column."""
    path = Path(suite_dir) / f"{spec.suite}.csv"
    if not path.exists():
        raise SpecError(f"suite file not found: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for column in ("order", "instance_id", "image"):
        if rows and column not in rows[0]:
            raise SpecError(f"{path}: suite has no '{column}' column")
    selected = []
    for row in rows:
        order = int(row["order"])
        if spec.row_start is not None and order < spec.row_start:
            continue
        if spec.row_stop is not None and order > spec.row_stop:
            continue
        selected.append(row)
    if not selected:
        raise SpecError(f"{path}: rows {spec.row_start}..{spec.row_stop} select nothing")
    return selected


def load_frame_content(path: str | Path) -> dict[str, dict[str, str]]:
    """The benchmark's own record per instance, one JSON object per line."""
    p = Path(path)
    if not p.exists():
        raise SpecError(
            f"frame content cache not found: {p}. Build it once from the benchmark parquet "
            "(see experiment/batches/README.md)."
        )
    by_id: dict[str, dict[str, str]] = {}
    with p.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                record = json.loads(line)
                by_id[record["instance_id"]] = record
    return by_id


def build_instances(rows: list[dict[str, str]], frame: dict[str, dict[str, str]]) -> str:
    """The driver's instance file, byte for byte the same for the same rows.

    Every field is the benchmark's, as a string; ``image`` is the suite's. Keys
    are sorted and non-ASCII is kept, so the digest of this text identifies
    the task content a batch ran on.
    """
    missing = [row["instance_id"] for row in rows if row["instance_id"] not in frame]
    if missing:
        raise SpecError(f"{len(missing)} suite instances are not in the frame content: {missing[:5]}")
    out = []
    for row in rows:
        record = dict(frame[row["instance_id"]])
        if record.get("repo") != row.get("repo", record.get("repo")):
            raise SpecError(
                f"{row['instance_id']}: suite says repo {row.get('repo')!r}, frame says {record.get('repo')!r}"
            )
        record["image"] = row["image"]
        out.append(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return "".join(out)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- card files --------------------------------------------------------------


def card_file_paths(spec: BatchSpec, local_opencollab_dir: str | Path) -> list[str]:
    """Repo-relative paths of the team file and every prompt it seats.

    Read from the local checkout only to learn the *names*; the bytes that
    matter are compared at the pinned commit and on the host.
    """
    if spec.cell is None:
        return []
    rel_yaml = f"configs/team.handoff.{spec.cell}.yaml"
    path = Path(local_opencollab_dir) / rel_yaml
    if not path.exists():
        raise SpecError(f"cell {spec.cell!r}: {path} does not exist")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    roles = (raw or {}).get("roles") or {}
    files = [rel_yaml]
    for role, entry in roles.items():
        prompt_file = (entry or {}).get("prompt_file")
        if not prompt_file:
            raise SpecError(f"cell {spec.cell!r}: role {role!r} has no prompt_file")
        files.append(f"configs/{prompt_file}")
    return files


# --- the command -------------------------------------------------------------


def driver_argv(spec: BatchSpec, host: HostConfig, limit: int | None = None) -> list[str]:
    """Exactly what the driver is started with, relative to ``host.workdir``."""
    argv = [
        host.python,
        "-m",
        DRIVER_MODULE,
        "--instances",
        spec.instances_file,
        "--out-dir",
        spec.name,
        "--arm",
        spec.arm,
    ]
    team_config = spec.team_config_relpath(host)
    if team_config is not None:
        argv += ["--team-config", team_config]
    timeout = int(spec.timeout) if float(spec.timeout).is_integer() else spec.timeout
    argv += [
        "--budget-per-seat",
        str(spec.budget_per_seat),
        "--max-steps",
        str(spec.max_steps),
        "--timeout",
        str(timeout),
        "--concurrency",
        str(spec.concurrency),
    ]
    if limit is not None:
        argv += ["--limit", str(limit)]
    return argv


def driver_env(spec: BatchSpec, host: HostConfig) -> dict[str, str]:
    """The environment the driver needs on top of a bare ``ssh`` shell.

    ``PYTHONPATH`` first: the host's venv imports an *installed* copy of
    OpenCollab from another checkout, so without this line the pinned code is
    never run and nothing reports it.
    """
    env = {
        "PYTHONPATH": host.pythonpath,
        "OPENCOLLAB_CONFIG_FILE": f"{host.workdir}/{host.opencollab_dir}/{spec.model_env}",
    }
    if host.proxy:
        env["http_proxy"] = host.proxy
        env["https_proxy"] = host.proxy
    env.update(spec.env)
    return env


def launch_script(spec: BatchSpec, host: HostConfig, limit: int | None = None) -> str:
    """A bash script that starts the batch detached and proves it is running."""
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in driver_env(spec, host).items())
    argv = " ".join(shlex.quote(a) for a in driver_argv(spec, host, limit))
    return "\n".join(
        [
            "set -u",
            f"cd {shlex.quote(host.workdir)}",
            f"setsid nohup env {env} {argv} < /dev/null >> {shlex.quote(spec.log_file)} 2>&1 &",
            "sleep 5",
            f'pgrep -af "{BATCH_PROCESS_PATTERN}" | grep -F -- {shlex.quote("--out-dir " + spec.name + " ")} '
            "| head -1 | cut -c1-120 || true",
            "",
        ]
    )
