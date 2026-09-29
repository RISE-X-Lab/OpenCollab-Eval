"""A batch file has to become the driver's exact command, and the pre-flight has to refuse."""

from __future__ import annotations

import json
import shlex
import subprocess
import textwrap
from pathlib import Path

import pytest

from opencollab_eval.commands import batch as batch_cli
from opencollab_eval.experiment import batch_remote
from opencollab_eval.experiment.batch_spec import (
    card_file_paths,
    load_host,
    load_spec,
)
from tests.support.paths import SOURCE_ROOT

ROOT = SOURCE_ROOT
EXPERIMENT = SOURCE_ROOT / "experiment"
PIN_OC = "b00f256995e321e910a988908842705177d4c0f4"
PIN_EVAL = "315a06c3df10dcde3d0218a3a3b6a6b157ee12b1"

# The command the 2026-09-02 batch was launched with by hand, from the log of that launch.
HAND_LAUNCHED_ARGV = [
    "/home/xuzhenhua/git/OpenCollab/.venv/bin/python",
    "-m",
    "opencollab_eval.generation.gen_prediction_batch",
    "--instances",
    "cmdplain30-instances.jsonl",
    "--out-dir",
    "cmdplain30",
    "--arm",
    "team",
    "--team-config",
    "OpenCollab/configs/team.handoff.cmd-plain.yaml",
    "--budget-per-seat",
    "2000000",
    "--max-steps",
    "100",
    "--timeout",
    "5400",
    "--concurrency",
    "6",
]
HAND_LAUNCHED_INSTANCES_SHA256 = "3dc013350ddd681237fe109bb9d5364d4cc4659c918a4e3ae8333e5f5084ba4f"


# --- fixtures: a tiny experiment dir and a git repo standing in for OpenCollab ---


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def oc_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "OpenCollab"
    (repo / "configs" / "handoff-experiment").mkdir(parents=True)
    (repo / "configs" / "team.handoff.x.yaml").write_text(
        "entry: analyst\nroles:\n  analyst: {prompt_file: handoff-experiment/analyst.x.md, tools: [bash]}\n"
        "  coder: {prompt_file: handoff-experiment/coder.md, tools: [bash]}\n",
        encoding="utf-8",
    )
    (repo / "configs" / "handoff-experiment" / "analyst.x.md").write_text("analyst card\n", encoding="utf-8")
    (repo / "configs" / "handoff-experiment" / "coder.md").write_text("coder card\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "cards")
    return repo, _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def experiment(
    tmp_path: Path, oc_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path | str]:
    """Create both pinned commits inside repositories owned by this fixture."""
    repo, sha = oc_repo
    eval_repo = tmp_path / "OpenCollab-Eval"
    eval_repo.mkdir()
    (eval_repo / "README.md").write_text("Synthetic evaluation checkout for batch tests.\n", encoding="utf-8")
    _git(eval_repo, "init", "-q")
    _git(eval_repo, "add", "README.md")
    _git(eval_repo, "commit", "-q", "-m", "\u521b\u5efa\u8bc4\u6d4b\u6d4b\u8bd5\u6837\u672c")
    eval_sha = _git(eval_repo, "rev-parse", "HEAD")
    # Run the production revision checks against the fixture's real Git objects.
    monkeypatch.setattr(batch_cli, "REPO_ROOT", eval_repo)
    exp = tmp_path / "experiment"
    (exp / "suite").mkdir(parents=True)
    (exp / "hosts").mkdir()
    (exp / "batches").mkdir()
    (exp / "suite" / "tiny.csv").write_text(
        "order,instance_id,repo,difficulty,image\n"
        "1,a__a-1,a/a,<15 min fix,img/a-1:latest\n"
        "2,b__b-2,b/b,1-4 hours,img/b-2:latest\n"
        "3,c__c-3,c/c,>4 hours,img/c-3:latest\n",
        encoding="utf-8",
    )
    frame = tmp_path / "frame.jsonl"
    frame.write_text(
        "".join(
            json.dumps(
                {"instance_id": i, "repo": r, "problem_statement": f"fix {i} é", "FAIL_TO_PASS": "[]"},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
            for i, r in (("a__a-1", "a/a"), ("b__b-2", "b/b"), ("c__c-3", "c/c"))
        ),
        encoding="utf-8",
    )
    (exp / "hosts" / "h.yaml").write_text(
        textwrap.dedent(
            f"""\
            name: h
            ssh: h
            workdir: /home/u/work
            python: /home/u/venv/bin/python
            opencollab_dir: OpenCollab
            eval_dir: OpenCollab-Eval
            proxy: http://proxy:8888
            docker_disk: /mnt
            min_free_gb: 10
            local_batches_dir: {tmp_path / "batches"}
            local_opencollab_dir: {repo}
            frame_content: {frame}
            """
        ),
        encoding="utf-8",
    )
    spec = exp / "batches" / "t1.yaml"
    spec.write_text(
        textwrap.dedent(
            f"""\
            name: t1
            host: h
            arm: team
            cell: x
            suite: tiny
            rows: {{start: 1, stop: 2}}
            budget_per_seat: 2000000
            max_steps: 100
            timeout: 5400
            concurrency: 2
            model_env: configs/.env
            env:
              OPENCOLLAB_LLM_STREAM_CHAT: "true"
              OPENCOLLAB_REASONING_EFFORT: "max"
              OPENCOLLAB_WRITE_NUDGE_MODE: "off"
            pins:
              opencollab: {sha}
              opencollab_eval: {eval_sha}
            """
        ),
        encoding="utf-8",
    )
    return {
        "dir": exp,
        "spec": spec,
        "repo": repo,
        "sha": sha,
        "eval_repo": eval_repo,
        "eval_sha": eval_sha,
        "frame": frame,
    }


def _spec_text(experiment: dict, old: str, new: str) -> str:
    text = Path(experiment["spec"]).read_text(encoding="utf-8")
    assert old in text, old
    return text.replace(old, new)


# --- spec loading -----------------------------------------------------------------


# --- instances --------------------------------------------------------------------


REAL_CACHE = Path("~/.cache/opencollab-eval/swebench-verified.jsonl").expanduser()


# --- the command ------------------------------------------------------------------


# --- pre-flight -------------------------------------------------------------------


def _good_facts(spec, host, cards: dict[str, str], instances_sha: str) -> str:
    lines = [
        f"OC_HEAD\t{spec.pins['opencollab']}",
        "OC_DIRTY\t0",
        f"EVAL_HEAD\t{spec.pins['opencollab_eval']}",
        "EVAL_DIRTY\t0",
        f"IMPORT_OC\t{host.workdir}/OpenCollab/opencollab/__init__.py",
        f"IMPORT_EVAL\t{host.workdir}/OpenCollab-Eval/src/opencollab_eval/__init__.py",
        *[f"CARD\t{rel}\t{sha}" for rel, sha in cards.items()],
        # What the host answers when every seat is given the pinned prompt.
        "DIGESTS\t"
        + json.dumps(
            {Path(rel).name.split(".")[0]: sha for rel, sha in cards.items() if not rel.endswith(".yaml")},
            sort_keys=True,
        ),
        "MODEL_ENV\tpresent",
        "ENDPOINT\t200",
        "MODEL\tdeepseek-v4-flash",
        "PROVIDER\topenai",
        "BASE_URL_SHA\tdeadbeef",
        "DISK_FREE_GB\t15",
        "DECOY_HIT\t1",
        "OUTDIR\tabsent",
        "REMOTE_INSTANCES_SHA\tabsent",
        "some prose the shell printed without a tab",
    ]
    return "\n".join(lines) + "\n"


def _checks(experiment: dict, facts: str) -> dict[str, batch_remote.Check]:
    spec = load_spec(experiment["spec"])
    host = load_host(experiment["dir"] / "hosts" / "h.yaml")
    cards = {
        rel: batch_cli.blob_sha256(experiment["repo"], experiment["sha"], rel)
        for rel in card_file_paths(spec, experiment["repo"])
    }
    parsed = batch_remote.parse_facts(facts)
    return {c.name: c for c in batch_remote.evaluate_preflight(spec, host, parsed, cards, "abc")}


def _facts(experiment: dict) -> str:
    spec = load_spec(experiment["spec"])
    host = load_host(experiment["dir"] / "hosts" / "h.yaml")
    cards = {
        rel: batch_cli.blob_sha256(experiment["repo"], experiment["sha"], rel)
        for rel in card_file_paths(spec, experiment["repo"])
    }
    return _good_facts(spec, host, cards, "abc")


# --- the CLI end to end with a fake host ---------------------------------------------


class FakeRemote:
    def __init__(self, facts: str) -> None:
        self.facts = facts
        self.scripts: list[str] = []
        self.copied: list[list[str]] = []

    def run(self, script: str, timeout: float = 0) -> str:
        self.scripts.append(script)
        if "DECOY_HIT" in script:
            return self.facts
        if "setsid nohup env" in script:
            return "4242 python -m opencollab_eval.generation.gen_prediction_batch --out-dir t1 \n"
        return ""

    def copy_to(self, local_paths, remote_dir: str) -> None:
        self.copied.append([str(p) for p in local_paths] + [remote_dir])

    def pull(self, remote_dir: str, local_dir: Path) -> None:
        raise AssertionError("not used here")


# --- the report ------------------------------------------------------------------------


def _agent_file(
    cell: Path, iid: str, aid: int, role: str, tokens: int, assistant: int, calls: list[str], terminal: str = ""
) -> None:
    d = cell / "logs-team" / iid / "trajectories" / "solver-x" / "team"
    d.mkdir(parents=True, exist_ok=True)
    messages = [
        {"role": "assistant", "tool_calls": [{"function": {"name": n}} for n in calls]} for _ in range(assistant)
    ]
    (d / f"agent_{aid}.json").write_text(
        json.dumps(
            {
                "aid": aid,
                "role": role,
                "session_state": {"used_tokens": tokens, "step_count": assistant, "terminal_reason": terminal},
                "messages": messages,
            }
        ),
        encoding="utf-8",
    )


# --- the host scripts, executed for real under bash ------------------------------------


@pytest.fixture
def fake_host(tmp_path: Path, oc_repo: tuple[Path, str]) -> dict:
    """A workdir laid out like the real one, with a python that answers the two import questions."""
    repo, sha = oc_repo
    work = tmp_path / "work"
    (work / "OpenCollab-Eval" / "src" / "opencollab_eval").mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", str(repo), str(work / "OpenCollab")], check=True)
    (work / "OpenCollab" / ".git" / "info" / "exclude").write_text("configs/.env\n", encoding="utf-8")
    _git(work / "OpenCollab-Eval", "init", "-q")
    (work / "OpenCollab-Eval" / "README").write_text("stand-in\n", encoding="utf-8")
    _git(work / "OpenCollab-Eval", "add", ".")
    _git(work / "OpenCollab-Eval", "commit", "-q", "-m", "stand-in")
    eval_sha = _git(work / "OpenCollab-Eval", "rev-parse", "HEAD")
    (work / "OpenCollab" / "configs" / ".env").write_text(
        "OPENCOLLAB_API_KEY=sk-verysecret\nOPENCOLLAB_BASE_URL=https://x.example/v1\n"
        "OPENCOLLAB_MODEL=fake-model\nOPENCOLLAB_PROVIDER=openai\n",
        encoding="utf-8",
    )
    # What a host answers when every seat really is given the pinned prompt.
    # Stand-in digests would make the check that compares them to the pin pass
    # on a host where nothing matched.
    seat_digests = {
        "analyst": batch_cli.blob_sha256(repo, sha, "configs/handoff-experiment/analyst.x.md"),
        "coder": batch_cli.blob_sha256(repo, sha, "configs/handoff-experiment/coder.md"),
    }
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    py = venv / "python"
    py.write_text(
        "#!/bin/bash\n"
        'case "$*" in\n'
        f"  *'import opencollab, opencollab_eval'*) echo {work}/OpenCollab/opencollab/__init__.py; "
        f"echo {work}/OpenCollab-Eval/src/opencollab_eval/__init__.py;;\n"
        f"  *declared_role_prompt_digests*) echo {shlex.quote(json.dumps(seat_digests, sort_keys=True))};;\n"
        # The endpoint probe, answered without a network: the real one was run
        # against the live host on 2026-09-20 and reported 200 for a good env,
        # 401 for a wrong key, 404 for a wrong model name and no-env for a
        # missing file, so all four of its branches have been seen.
        '  *urllib.request*) printf "ENDPOINT\\t200\\n";;\n'
        '  *) exec python3 "$@";;\n'
        "esac\n",
        encoding="utf-8",
    )
    py.chmod(0o755)
    host_file = tmp_path / "fake.yaml"
    host_file.write_text(
        textwrap.dedent(
            f"""\
            name: fake
            ssh: nowhere
            workdir: {work}
            python: {py}
            opencollab_dir: OpenCollab
            eval_dir: OpenCollab-Eval
            docker_disk: /
            min_free_gb: 0
            local_batches_dir: {tmp_path / "batches"}
            local_opencollab_dir: {repo}
            frame_content: {tmp_path / "none.jsonl"}
            """
        ),
        encoding="utf-8",
    )
    return {
        "work": work,
        "sha": sha,
        "eval_sha": eval_sha,
        "host_file": host_file,
        "repo": repo,
        "seat_digests": seat_digests,
    }


def _spec_for_fake_host(experiment: dict, fake_host: dict):
    path = experiment["dir"] / "batches" / "fake.yaml"
    path.write_text(_spec_text(experiment, experiment["eval_sha"], fake_host["eval_sha"]), encoding="utf-8")
    return load_spec(path)


def _bash(script: str) -> str:
    return subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True, timeout=60, check=False).stdout


# --- rungs, pins, and arms without a team -------------------------------------------------


# --- sync: bringing the host's checkouts to the pins ------------------------------
#
# Pre-flight names a pin mismatch and stops; every batch so far was preceded by
# a hand-run fetch and checkout over ssh. The step is now a command, which means
# its two refusals have to be the tested part: a checkout that moves the code
# under a running batch, and a checkout onto a tree somebody left modified.


class SyncRemote:
    """Answers the guard script from a script, and records what it was sent."""

    def __init__(self, guard: str, sync: str = "") -> None:
        self.guard = guard
        self.sync = sync
        self.scripts: list[str] = []

    def run(self, script: str, timeout: float = 0) -> str:
        self.scripts.append(script)
        return self.sync if "sync_repo()" in script else self.guard

    def copy_to(self, local_paths, remote_dir: str) -> None:
        raise AssertionError("sync copies nothing")

    def pull(self, remote_dir: str, local_dir: Path) -> None:
        raise AssertionError("not used here")


def _sync(experiment: dict, remote: SyncRemote, command: str = "sync") -> int:
    return batch_cli.main(
        ["--experiment-dir", str(experiment["dir"]), command, str(experiment["spec"])],
        remote_factory=lambda h: remote,
    )


def _guard(*, running: str = "", dirty: str = "0", decoy: str = "1") -> str:
    return (
        "OC_BEFORE\told\n"
        "EV_BEFORE\told\n"
        f"OC_DIRTY\t{dirty}\n"
        f"EV_DIRTY\t{dirty}\n" + (f"RUNNING\t{running}\n" if running else "") + f"DECOY_HIT\t{decoy}\n"
    )


def _synced(experiment: dict) -> str:
    return f"OC_AFTER\t{experiment['sha']}\nEV_AFTER\t{experiment['eval_sha']}\n"


def _guard_facts_with_a_driver(tmp_path: Path, driver_pythonpath: str | None, host_file: str) -> list:
    """Run the real guard script here, beside a real process that looks like a driver.

    The attribution happens in the shell on the host (it reads the driver's
    environment), so a fake remote cannot test it. The fake driver's command
    line carries the batch pattern and a mark unique to this test; its
    PYTHONPATH is the one a launch from some checkout would set.
    """
    import dataclasses
    import os
    import time

    host = dataclasses.replace(load_host(EXPERIMENT / "hosts" / host_file), workdir=str(tmp_path))
    mark = f"guard-attribution-test-{os.getpid()}"
    argv0 = f"python -m opencollab_eval.generation.gen_prediction_batch --out-dir {mark}"
    env = {"PATH": os.environ["PATH"]}
    if driver_pythonpath is not None:
        env["PYTHONPATH"] = driver_pythonpath
    driver = subprocess.Popen(["bash", "-c", f'exec -a "{argv0}" sleep 60'], env=env)
    try:
        time.sleep(0.5)
        out = subprocess.run(
            ["bash", "-s"],
            input=batch_remote.sync_guard_script(host),
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout
    finally:
        driver.kill()
        driver.wait()
    return [f for f in batch_remote.parse_facts(out) if len(f) > 1 and mark in f[1]]


def _checkout_pythonpath(tmp_path: Path, host_file: str) -> str:
    import dataclasses

    return dataclasses.replace(load_host(EXPERIMENT / "hosts" / host_file), workdir=str(tmp_path)).pythonpath


_NEEDS_PROC = pytest.mark.skipif(
    not Path("/proc/self/environ").exists() or subprocess.run(["which", "pgrep"], capture_output=True).returncode,
    reason="the guard reads /proc/<pid>/environ and uses pgrep",
)


# --- score: the gold control, and the counts read before anything is spent --------
#
# On 2026-09-07 a cell reported a task as unresolvable with a note about a bad
# evaluation environment. The environment was a machine with no route to the
# package index; the same task's reference patch resolves where there is one.
# The control that would have caught it is scoring the reference patches in the
# same session, on the same host, against the same dataset -- so `score` runs
# them first and refuses to read anything else until they all resolve.


class ScoreRemote:
    """Answers the guard, the dataset filter and the launch, and records them."""

    def __init__(self, guard: str, dataset: str = "DATASET_ROWS\t2\n", launch: str = "") -> None:
        self.guard = guard
        self.dataset = dataset
        self.launch = launch
        self.scripts: list[str] = []
        self.copied: list[Path] = []

    def run(self, script: str, timeout: float = 0) -> str:
        self.scripts.append(script)
        # The guard script names the harness process too, so the three are
        # told apart by what only one of them carries.
        if "DATASET_ROWS" in script:
            return self.dataset
        if "--run_id" in script:
            return self.launch
        return self.guard

    def copy_to(self, local_paths, remote_dir: str) -> None:
        self.copied.extend(local_paths)

    def pull(self, remote_dir: str, local_dir: Path) -> None:
        raise AssertionError("not used here")


def _score_guard(
    *,
    preds: str = "present",
    rows: str = "2",
    empty: str = "0",
    swebench: str = "5.0.2",
    disk: str = "500",
    decoy: str = "1",
    running: str = "",
) -> str:
    return (
        f"PREDS\t{preds}\nPREDS_ROWS\t{rows}\nPREDS_EMPTY\t{empty}\n"
        f"SWEBENCH\t{swebench}\nSCORE_DIR\tabsent\n"
        + (f"RUNNING\t{running}\n" if running else "")
        + f"DECOY_HIT\t{decoy}\nDISK_FREE_GB\t{disk}\n"
    )


def _with_scoring_dataset(experiment: dict) -> None:
    host = Path(experiment["dir"]) / "hosts" / "h.yaml"
    host.write_text(host.read_text(encoding="utf-8") + "scoring_dataset: /home/u/ds.jsonl\n", encoding="utf-8")


def _score(experiment: dict, remote: ScoreRemote, *extra: str) -> int:
    return batch_cli.main(
        ["--experiment-dir", str(experiment["dir"]), "score", str(experiment["spec"]), *extra],
        remote_factory=lambda h: remote,
    )


# --- derived specs ----------------------------------------------------------------
