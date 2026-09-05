"""A batch file has to become the driver's exact command, and the pre-flight has to refuse."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import textwrap
from pathlib import Path

import pytest

from opencollab_eval.commands import batch as batch_cli
from opencollab_eval.experiment import batch_remote, cell_report
from opencollab_eval.experiment.batch_spec import (
    SpecError,
    build_instances,
    card_file_paths,
    driver_argv,
    driver_env,
    launch_script,
    load_host,
    load_spec,
    spec_digest,
    suite_rows,
)

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "experiment"
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
def experiment(tmp_path: Path, oc_repo: tuple[Path, str]) -> dict[str, Path | str]:
    repo, sha = oc_repo
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
              opencollab_eval: {PIN_EVAL}
            """
        ),
        encoding="utf-8",
    )
    return {"dir": exp, "spec": spec, "repo": repo, "sha": sha, "frame": frame}


def _spec_text(experiment: dict, old: str, new: str) -> str:
    text = Path(experiment["spec"]).read_text(encoding="utf-8")
    assert old in text, old
    return text.replace(old, new)


# --- spec loading -----------------------------------------------------------------


def test_spec_rejects_unquoted_boolean_switch(experiment: dict) -> None:
    path = experiment["dir"] / "batches" / "bad.yaml"
    path.write_text(
        _spec_text(experiment, 'OPENCOLLAB_WRITE_NUDGE_MODE: "off"', "OPENCOLLAB_WRITE_NUDGE_MODE: off"),
        encoding="utf-8",
    )
    with pytest.raises(SpecError, match="parsed as a boolean"):
        load_spec(path)


def test_spec_requires_all_three_switches(experiment: dict) -> None:
    path = experiment["dir"] / "batches" / "bad.yaml"
    path.write_text(_spec_text(experiment, '  OPENCOLLAB_REASONING_EFFORT: "max"\n', ""), encoding="utf-8")
    with pytest.raises(SpecError, match="OPENCOLLAB_REASONING_EFFORT"):
        load_spec(path)


def test_spec_requires_full_sha(experiment: dict) -> None:
    path = experiment["dir"] / "batches" / "bad.yaml"
    path.write_text(_spec_text(experiment, PIN_EVAL, PIN_EVAL[:7]), encoding="utf-8")
    with pytest.raises(SpecError, match="40-character"):
        load_spec(path)


def test_spec_cell_must_match_arm(experiment: dict) -> None:
    path = experiment["dir"] / "batches" / "bad.yaml"
    path.write_text(_spec_text(experiment, "arm: team", "arm: single"), encoding="utf-8")
    with pytest.raises(SpecError, match="takes no 'cell'"):
        load_spec(path)
    path.write_text(_spec_text(experiment, "cell: x\n", ""), encoding="utf-8")
    with pytest.raises(SpecError, match="needs a 'cell'"):
        load_spec(path)


def test_spec_digest_ignores_concurrency_only(experiment: dict) -> None:
    base = load_spec(experiment["spec"])
    path = experiment["dir"] / "batches" / "c.yaml"
    path.write_text(_spec_text(experiment, "concurrency: 2", "concurrency: 6"), encoding="utf-8")
    assert spec_digest(load_spec(path)) == spec_digest(base)
    path.write_text(_spec_text(experiment, "budget_per_seat: 2000000", "budget_per_seat: 1000000"), encoding="utf-8")
    assert spec_digest(load_spec(path)) != spec_digest(base)


# --- instances --------------------------------------------------------------------


def test_instances_are_the_slice_with_the_suite_image(experiment: dict) -> None:
    spec = load_spec(experiment["spec"])
    rows = suite_rows(spec, experiment["dir"] / "suite")
    assert [r["instance_id"] for r in rows] == ["a__a-1", "b__b-2"]
    records = [json.loads(line) for line in Path(experiment["frame"]).read_text(encoding="utf-8").splitlines()]
    frame = {record["instance_id"]: record for record in records}
    text = build_instances(rows, frame)
    lines = text.splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["image"] == "img/a-1:latest"
    assert first["problem_statement"] == "fix a__a-1 é"
    assert "é" in lines[0]  # not escaped
    assert lines[0] == json.dumps(first, ensure_ascii=False, sort_keys=True)


def test_missing_frame_record_is_an_error(experiment: dict) -> None:
    spec = load_spec(experiment["spec"])
    rows = suite_rows(spec, experiment["dir"] / "suite")
    with pytest.raises(SpecError, match="not in the frame"):
        build_instances(rows, {})


REAL_CACHE = Path("~/.cache/opencollab-eval/swebench-verified.jsonl").expanduser()


@pytest.mark.skipif(not REAL_CACHE.exists(), reason="frame content cache not built on this machine")
def test_real_spec_reproduces_the_hand_built_instance_file() -> None:
    from opencollab_eval.experiment.batch_spec import load_frame_content, sha256_text

    spec = load_spec(EXPERIMENT / "batches" / "cmdplain30.yaml")
    rows = suite_rows(spec, EXPERIMENT / "suite")
    text = build_instances(rows, load_frame_content(REAL_CACHE))
    assert sha256_text(text) == HAND_LAUNCHED_INSTANCES_SHA256


# --- the command ------------------------------------------------------------------


def test_real_spec_reproduces_the_hand_launched_command() -> None:
    spec = load_spec(EXPERIMENT / "batches" / "cmdplain30.yaml")
    host = load_host(EXPERIMENT / "hosts" / "gpu3.yaml")
    assert driver_argv(spec, host) == HAND_LAUNCHED_ARGV
    env = driver_env(spec, host)
    assert (
        env["PYTHONPATH"]
        == "/home/xuzhenhua/oc-team-smoke/OpenCollab:/home/xuzhenhua/oc-team-smoke/OpenCollab-Eval/src"
    )
    assert env["OPENCOLLAB_CONFIG_FILE"] == "/home/xuzhenhua/oc-team-smoke/OpenCollab/configs/.env"
    assert env["https_proxy"] == "http://172.16.200.37:8888"
    assert env["OPENCOLLAB_WRITE_NUDGE_MODE"] == "off"
    assert spec.pins == {"opencollab": PIN_OC, "opencollab_eval": PIN_EVAL}


def test_launch_script_is_detached_and_carries_the_switches(experiment: dict) -> None:
    spec = load_spec(experiment["spec"])
    host = load_host(experiment["dir"] / "hosts" / "h.yaml")
    script = launch_script(spec, host, limit=3)
    assert "setsid nohup env " in script
    assert "< /dev/null >> t1.log 2>&1 &" in script
    assert "--limit 3" in script
    for key in (
        "OPENCOLLAB_LLM_STREAM_CHAT=true",
        "OPENCOLLAB_REASONING_EFFORT=max",
        "OPENCOLLAB_WRITE_NUDGE_MODE=off",
    ):
        assert key in script
    assert "PYTHONPATH=/home/u/work/OpenCollab:/home/u/work/OpenCollab-Eval/src" in script


def test_card_files_follow_prompt_file(experiment: dict) -> None:
    spec = load_spec(experiment["spec"])
    assert card_file_paths(spec, experiment["repo"]) == [
        "configs/team.handoff.x.yaml",
        "configs/handoff-experiment/analyst.x.md",
        "configs/handoff-experiment/coder.md",
    ]


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
        'DIGESTS\t{"analyst": "aa", "coder": "bb"}',
        "MODEL_ENV\tpresent",
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


def test_preflight_passes_when_the_host_matches(experiment: dict) -> None:
    checks = _checks(experiment, _facts(experiment))
    failed = [c for c in checks.values() if not c.ok]
    assert failed == [], [(c.name, c.detail) for c in failed]


@pytest.mark.parametrize(
    ("mutation", "failing"),
    [
        (("OC_HEAD\t", "OC_HEAD\t0000000000000000000000000000000000000000#"), "pin opencollab"),
        (("OC_DIRTY\t0", "OC_DIRTY\t2"), "clean opencollab"),
        (("EVAL_DIRTY\t0", "EVAL_DIRTY\t1"), "clean opencollab_eval"),
        (
            ("/home/u/work/OpenCollab/opencollab/__init__.py", "/nfs/other/OpenCollab/opencollab/__init__.py"),
            "import opencollab from pinned checkout",
        ),
        (("DISK_FREE_GB\t15", "DISK_FREE_GB\t3"), "docker disk free"),
        (("DECOY_HIT\t1", "DECOY_HIT\t0"), "running-batch check sees a planted process"),
        (("OUTDIR\tabsent", "OUTDIR\tno-batchjson"), "out-dir"),
        (("OUTDIR\tabsent", "OUTDIR\t1111111111111111111111111111111111111111111111111111111111111111"), "out-dir"),
        (("MODEL\tdeepseek-v4-flash", "MODEL\t"), "model named"),
        (("REMOTE_INSTANCES_SHA\tabsent", "REMOTE_INSTANCES_SHA\tzzz"), "instance file on host"),
    ],
)
def test_preflight_fails_on_each_drift(experiment: dict, mutation: tuple[str, str], failing: str) -> None:
    facts = _facts(experiment).replace(*mutation)
    checks = _checks(experiment, facts)
    assert not checks[failing].ok, checks[failing]


def test_preflight_fails_when_card_bytes_differ_from_the_pin(experiment: dict) -> None:
    facts = _facts(experiment)
    spec = load_spec(experiment["spec"])
    real = batch_cli.blob_sha256(experiment["repo"], experiment["sha"], "configs/handoff-experiment/analyst.x.md")
    facts = facts.replace(real, hashlib.sha256(b"edited on the host").hexdigest())
    checks = _checks(experiment, facts)
    assert not checks["card bytes configs/handoff-experiment/analyst.x.md"].ok
    assert checks["card bytes configs/team.handoff.x.yaml"].ok
    assert spec.cell == "x"


def test_preflight_fails_on_missing_image_and_same_outdir_driver(experiment: dict) -> None:
    facts = (
        _facts(experiment)
        + "IMAGE_MISSING\timg/b-2:latest\n"
        + "RUNNING\t123 python -m x --instances t1-instances.jsonl --out-dir t1 --arm team\n"
    )
    checks = _checks(experiment, facts)
    assert not checks["task images present"].ok
    assert not checks["no driver already writing this out-dir"].ok


def test_other_batch_running_is_a_warning_not_a_failure(experiment: dict) -> None:
    facts = _facts(experiment) + "RUNNING\t123 python -m x --instances o-instances.jsonl --out-dir other --arm team\n"
    checks = _checks(experiment, facts)
    assert checks["no driver already writing this out-dir"].ok
    assert checks["other batches running on the host"].warn


def test_resume_into_own_outdir_is_allowed(experiment: dict) -> None:
    spec = load_spec(experiment["spec"])
    facts = _facts(experiment).replace("OUTDIR\tabsent", f"OUTDIR\t{spec_digest(spec)}")
    assert _checks(experiment, facts)["out-dir"].ok


def test_preflight_script_never_reads_the_key(experiment: dict) -> None:
    spec = load_spec(experiment["spec"])
    host = load_host(experiment["dir"] / "hosts" / "h.yaml")
    script = batch_remote.preflight_script(spec, host, ["configs/team.handoff.x.yaml"], ["img/a-1:latest"])
    assert "OPENCOLLAB_API_KEY" not in script
    assert 'cat "$ME"' not in script and "cat $ME" not in script
    assert 'grep -E "^OPENCOLLAB_MODEL="' in script
    url_line = next(line for line in script.splitlines() if "^OPENCOLLAB_BASE_URL=" in line)
    assert "sha256sum" in url_line and "printf" in url_line
    assert "[o]pencollab_eval.generation.gen_prediction_batch" in script


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


def test_cli_plan_writes_inputs_and_record(experiment: dict, capsys) -> None:
    rc = batch_cli.main(
        ["--experiment-dir", str(experiment["dir"]), "plan", str(experiment["spec"])], remote_factory=lambda h: None
    )
    assert rc == 0
    launch_dir = Path(load_host(experiment["dir"] / "hosts" / "h.yaml").local_batches_dir) / "t1.launch"
    record = json.loads((launch_dir / "batch.json").read_text(encoding="utf-8"))
    assert record["instances"]["count"] == 2
    assert record["spec"]["pins"]["opencollab"] == experiment["sha"]
    assert set(record["expected_card_sha256"]) == {
        "configs/team.handoff.x.yaml",
        "configs/handoff-experiment/analyst.x.md",
        "configs/handoff-experiment/coder.md",
    }
    assert (launch_dir / "t1-instances.jsonl").read_text(encoding="utf-8").count("\n") == 2
    assert "--out-dir t1" in capsys.readouterr().out


def test_cli_launch_refuses_on_a_failed_check_and_copies_nothing(experiment: dict) -> None:
    facts = _facts(experiment).replace("OC_DIRTY\t0", "OC_DIRTY\t1")
    remote = FakeRemote(facts)
    rc = batch_cli.main(
        ["--experiment-dir", str(experiment["dir"]), "launch", str(experiment["spec"]), "--limit", "1"],
        remote_factory=lambda h: remote,
    )
    assert rc == 1
    assert remote.copied == []
    assert not any("setsid nohup env" in s for s in remote.scripts)


def test_cli_launch_copies_data_then_starts_detached(experiment: dict, capsys) -> None:
    remote = FakeRemote(_facts(experiment))
    rc = batch_cli.main(
        ["--experiment-dir", str(experiment["dir"]), "launch", str(experiment["spec"]), "--limit", "1"],
        remote_factory=lambda h: remote,
    )
    assert rc == 0, capsys.readouterr().out
    assert any(c[0].endswith("t1-instances.jsonl") and c[-1] == "/home/u/work" for c in remote.copied)
    assert any(c[0].endswith("batch.json") and c[-1] == "/home/u/work/t1" for c in remote.copied)
    launch = [s for s in remote.scripts if "setsid nohup env" in s]
    assert len(launch) == 1 and "--limit 1" in launch[0]
    record = json.loads(
        (
            Path(load_host(experiment["dir"] / "hosts" / "h.yaml").local_batches_dir) / "t1.launch" / "batch.json"
        ).read_text(encoding="utf-8")
    )
    assert record["host"]["model"] == "deepseek-v4-flash"
    assert record["host"]["role_prompt_sha256"] == {"analyst": "aa", "coder": "bb"}
    assert record["launches"][0]["limit"] == 1
    assert "API_KEY" not in json.dumps(record)


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


def test_report_counts_delivery_over_valid_runs_only(tmp_path: Path) -> None:
    cell = tmp_path / "cell"
    cell.mkdir()
    metrics = [
        {
            "instance_id": "a",
            "run_summary": {"status": "completed", "tokens": 100, "steps": 5},
            "role_prompt_sha256": {"analyst": "aa"},
            "team_config_path": "x.yaml",
            "tree_snapshots": [{"at": "handoff"}],
        },
        {
            "instance_id": "b",
            "run_summary": {"status": "stopped", "reason": "budget", "tokens": 200, "steps": 9},
            "role_prompt_sha256": {"analyst": "aa"},
            "team_config_path": "x.yaml",
        },
        {
            "instance_id": "c",
            "run_summary": {"status": "failed", "reason": "provider", "tokens": 0, "steps": 0},
            "role_prompt_sha256": {"analyst": "aa"},
            "team_config_path": "x.yaml",
        },
    ]
    (cell / "metrics.jsonl").write_text("".join(json.dumps(m) + "\n" for m in metrics), encoding="utf-8")
    _agent_file(cell, "a", 0, "analyst", 60, 3, ["apply_patch", "message_agent"])
    _agent_file(cell, "a", 1, "coder", 40, 2, ["apply_patch"])
    _agent_file(cell, "b", 0, "analyst", 200, 4, ["file_write"], terminal="budget_exhausted")
    _agent_file(cell, "b", 1, "coder", 0, 0, [])  # seated, never spent: not a delivery
    rows = cell_report.run_rows(cell, "team")
    summary = cell_report.summarize(rows, expected_card="aa")
    assert summary["valid"] == 2 and summary["invalid"] == ["c"]
    assert summary["delivered"] == 1 and summary["alpha"] == 0.5
    assert summary["cap_hit"] == ["b"]
    assert summary["card_matches_expected"] is True
    text = cell_report.render(rows, summary, [])
    assert "delivered 1/2 valid" in text and "[excluded: provider]" in text
    lo, hi = summary["ci95"]
    assert 0.01 < lo < 0.03 and 0.97 < hi < 0.99
    ordered, missing = cell_report.order_rows(rows, None)
    assert missing == [] and len(ordered) == 3


def test_clopper_pearson_known_values() -> None:
    assert cell_report.clopper_pearson(0, 20)[1] == pytest.approx(0.168, abs=0.001)
    assert cell_report.clopper_pearson(3, 3)[0] == pytest.approx(0.292, abs=0.001)
    assert cell_report.clopper_pearson(27, 40)[0] > 0.5
    assert cell_report.clopper_pearson(26, 40)[0] < 0.5
    assert all(math.isnan(v) for v in cell_report.clopper_pearson(0, 0))  # no denominator, no interval


# --- the host scripts, executed for real under bash ------------------------------------


def test_decoy_carries_the_unbracketed_pattern() -> None:
    from opencollab_eval.experiment.batch_spec import BATCH_PROCESS_PATTERN

    plain = BATCH_PROCESS_PATTERN.replace("[o]", "o")
    assert plain in batch_remote.DECOY_MARK


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
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    py = venv / "python"
    py.write_text(
        "#!/bin/bash\n"
        'case "$*" in\n'
        f"  *'import opencollab, opencollab_eval'*) echo {work}/OpenCollab/opencollab/__init__.py; "
        f"echo {work}/OpenCollab-Eval/src/opencollab_eval/__init__.py;;\n"
        '  *declared_role_prompt_digests*) echo \'{"analyst": "aa", "coder": "bb"}\';;\n'
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
    return {"work": work, "sha": sha, "eval_sha": eval_sha, "host_file": host_file, "repo": repo}


def _spec_for_fake_host(experiment: dict, fake_host: dict):
    path = experiment["dir"] / "batches" / "fake.yaml"
    path.write_text(_spec_text(experiment, PIN_EVAL, fake_host["eval_sha"]), encoding="utf-8")
    return load_spec(path)


def _bash(script: str) -> str:
    return subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True, timeout=60, check=False).stdout


def test_preflight_script_runs_under_bash_and_passes_on_a_matching_host(experiment: dict, fake_host: dict) -> None:
    spec = _spec_for_fake_host(experiment, fake_host)
    host = load_host(fake_host["host_file"])
    cards = {
        rel: batch_cli.blob_sha256(fake_host["repo"], fake_host["sha"], rel)
        for rel in card_file_paths(spec, fake_host["repo"])
    }
    out = _bash(batch_remote.preflight_script(spec, host, list(cards), images=[]))
    assert "sk-verysecret" not in out
    facts = batch_remote.parse_facts(out)
    checks = {c.name: c for c in batch_remote.evaluate_preflight(spec, host, facts, cards, "abc")}
    failed = [(c.name, c.detail) for c in checks.values() if not c.ok]
    assert failed == [], out
    record = batch_remote.facts_to_record(facts)
    assert record["model"] == "fake-model" and record["provider"] == "openai"
    assert record["base_url_sha256"] == hashlib.sha256(b"https://x.example/v1").hexdigest()
    assert record["card_sha256"] == cards
    assert record["role_prompt_sha256"] == {"analyst": "aa", "coder": "bb"}


def test_preflight_script_sees_an_edited_card_and_a_dirty_tree(experiment: dict, fake_host: dict) -> None:
    spec = _spec_for_fake_host(experiment, fake_host)
    host = load_host(fake_host["host_file"])
    cards = {
        rel: batch_cli.blob_sha256(fake_host["repo"], fake_host["sha"], rel)
        for rel in card_file_paths(spec, fake_host["repo"])
    }
    card = fake_host["work"] / "OpenCollab" / "configs" / "handoff-experiment" / "analyst.x.md"
    card.write_text("analyst card, edited on the host\n", encoding="utf-8")
    facts = batch_remote.parse_facts(_bash(batch_remote.preflight_script(spec, host, list(cards), images=[])))
    checks = {c.name: c for c in batch_remote.evaluate_preflight(spec, host, facts, cards, "abc")}
    assert not checks["card bytes configs/handoff-experiment/analyst.x.md"].ok
    assert not checks["clean opencollab"].ok
    assert checks["card bytes configs/handoff-experiment/coder.md"].ok


def test_status_script_counts_rows_under_bash(experiment: dict, fake_host: dict) -> None:
    spec = load_spec(experiment["spec"])
    host = load_host(fake_host["host_file"])
    out = fake_host["work"] / spec.name
    out.mkdir()
    (out / "metrics.jsonl").write_text(
        json.dumps({"instance_id": "a", "run_summary": {"status": "completed"}})
        + "\n"
        + json.dumps({"instance_id": "b", "run_summary": {"status": "stopped"}})
        + "\n"
        + json.dumps({"instance_id": "c", "run_summary": {"status": "completed"}})
        + "\n",
        encoding="utf-8",
    )
    (out / "preds-team.jsonl").write_text("{}\n{}\n{}\n", encoding="utf-8")
    (fake_host["work"] / spec.instances_file).write_text("{}\n" * 5, encoding="utf-8")
    (fake_host["work"] / spec.log_file).write_text("line one\nline two\n", encoding="utf-8")
    facts = batch_remote.parse_facts(_bash(batch_remote.status_script(spec, host)))
    assert batch_remote.fact(facts, "ALIVE") == "0"
    assert batch_remote.fact(facts, "TOTAL") == "5"
    lines = {p[0]: p[1] for p in batch_remote.facts_all(facts, "LINES")}
    assert lines == {"manifest.jsonl": "0", "preds-team.jsonl": "3", "metrics.jsonl": "3"}
    statuses = {p[0]: p[1] for p in batch_remote.facts_all(facts, "STATUS")}
    assert statuses == {"completed": "2", "stopped": "1"}
    assert [p[0] for p in batch_remote.facts_all(facts, "LOGTAIL")] == ["line one", "line two"]


# --- rungs, pins, and arms without a team -------------------------------------------------


def test_rung_derives_the_cell_and_refuses_a_mismatch(experiment: dict) -> None:
    from opencollab_eval.experiment.batch_spec import RUNG_CELLS

    path = experiment["dir"] / "batches" / "r.yaml"
    path.write_text(_spec_text(experiment, "cell: x\n", "rung: plain\n"), encoding="utf-8")
    spec = load_spec(path)
    assert (spec.rung, spec.cell) == ("plain", "cmd-plain")
    path.write_text(_spec_text(experiment, "cell: x\n", "rung: prohibit\ncell: cmd-plain\n"), encoding="utf-8")
    with pytest.raises(SpecError, match="must come from that rung's card"):
        load_spec(path)
    path.write_text(_spec_text(experiment, "cell: x\n", "rung: strong\n"), encoding="utf-8")
    with pytest.raises(SpecError, match="not one of"):
        load_spec(path)
    path.write_text(_spec_text(experiment, "arm: team\ncell: x\n", "arm: single\nrung: plain\n"), encoding="utf-8")
    with pytest.raises(SpecError, match="has none"):
        load_spec(path)
    assert RUNG_CELLS == {
        "primary": "facts-v2",
        "opt-out": "cmd-optout",
        "bare": "cmd-bare",
        "plain": "cmd-plain",
        "prohibit": "cmd-prohibit",
    }


def test_checked_in_specs_name_rung_and_cell_consistently() -> None:
    for path in sorted((EXPERIMENT / "batches").glob("*.yaml")):
        spec = load_spec(path)
        if spec.arm == "team":
            assert spec.rung is not None, f"{path.name}: a team spec names its rung"
            assert spec.cell is not None
        assert (EXPERIMENT / "hosts" / f"{spec.host}.yaml").exists()
        assert (EXPERIMENT / "suite" / f"{spec.suite}.csv").exists()


def test_plan_refuses_a_pin_that_is_not_a_local_commit(experiment: dict, capsys) -> None:
    path = experiment["dir"] / "batches" / "p.yaml"
    path.write_text(_spec_text(experiment, PIN_EVAL, "abcdef0123" * 4), encoding="utf-8")
    rc = batch_cli.main(["--experiment-dir", str(experiment["dir"]), "plan", str(path)], remote_factory=lambda h: None)
    assert rc == 2
    assert "not a commit" in capsys.readouterr().err


def test_report_for_a_single_arm_computes_no_delivery(tmp_path: Path) -> None:
    cell = tmp_path / "cell"
    cell.mkdir()
    metrics = [
        {
            "instance_id": "a",
            "run_summary": {"status": "completed", "tokens": 100, "steps": 5},
            "submitted_patch_chars": 40,
        },
        {"instance_id": "b", "run_summary": {"status": "failed", "reason": "provider", "tokens": 0, "steps": 0}},
    ]
    (cell / "metrics.jsonl").write_text("".join(json.dumps(m) + "\n" for m in metrics), encoding="utf-8")
    rows = cell_report.run_rows(cell, "single")
    summary = cell_report.summarize(rows, team=False)
    assert summary["delivered"] is None and summary["alpha"] is None and summary["ci95"] is None
    assert summary["valid"] == 1 and summary["invalid"] == ["b"]
    text = cell_report.render(rows, summary, [])
    assert "delivered" not in text and "team-arm quantity" in text and "[excluded: provider]" in text


def test_plan_after_preflight_keeps_the_host_facts(experiment: dict) -> None:
    remote = FakeRemote(_facts(experiment))
    args = ["--experiment-dir", str(experiment["dir"])]
    assert batch_cli.main([*args, "preflight", str(experiment["spec"])], remote_factory=lambda h: remote) == 0
    assert batch_cli.main([*args, "plan", str(experiment["spec"])], remote_factory=lambda h: None) == 0
    record_path = Path(load_host(experiment["dir"] / "hosts" / "h.yaml").local_batches_dir) / "t1.launch" / "batch.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["host"]["model"] == "deepseek-v4-flash"
    assert (
        batch_cli.main([*args, "launch", str(experiment["spec"]), "--limit", "1"], remote_factory=lambda h: remote) == 0
    )
    assert batch_cli.main([*args, "launch", str(experiment["spec"])], remote_factory=lambda h: remote) == 0
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert [launch["limit"] for launch in record["launches"]] == [1, None]
    assert batch_cli.main([*args, "plan", str(experiment["spec"])], remote_factory=lambda h: None) == 0
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert len(record["launches"]) == 2 and "host" in record


# --- retries: a second attempt at the instances an endpoint dropped -----------------------

#: The digest of a checked-in spec, frozen. ``retry_of`` had to enter the batch
#: identity, and a new key with a ``None`` value would have changed the digest of
#: every spec already on disk -- which is the pre-flight's own test for "this
#: out-dir holds a different batch". This pins the answer for a spec that names
#: no retry, so the field can only ever change the identity of a spec that uses it.
CMDPLAIN30_DIGEST = "4fb8ac66259c434e9db5ae9649627ef063bf70e031cdd1b9c7a0c8513df30caa"


def test_retry_of_changes_the_identity_only_for_the_specs_that_use_it(experiment: dict) -> None:
    from opencollab_eval.experiment.batch_spec import spec_identity

    assert spec_digest(load_spec(EXPERIMENT / "batches" / "cmdplain30.yaml")) == CMDPLAIN30_DIGEST
    base = load_spec(experiment["spec"])
    assert "retry_of" not in spec_identity(base)
    path = experiment["dir"] / "batches" / "t1r.yaml"
    path.write_text(
        _spec_text(experiment, "name: t1", "name: t1r\nretry_of: t1").replace(
            "rows: {start: 1, stop: 2}", "rows: {start: 2, stop: 2}"
        ),
        encoding="utf-8",
    )
    retry = load_spec(path)
    assert retry.retry_of == "t1"
    assert spec_identity(retry)["retry_of"] == "t1"
    assert spec_digest(retry) != spec_digest(base)


def _plan(experiment: dict, path: Path) -> int:
    return batch_cli.main(
        ["--experiment-dir", str(experiment["dir"]), "plan", str(path)], remote_factory=lambda h: None
    )


def _retry_spec(experiment: dict, name: str, rows: str, edit: tuple[str, str] | None = None) -> Path:
    text = _spec_text(experiment, "name: t1", f"name: {name}\nretry_of: t1").replace(
        "rows: {start: 1, stop: 2}", rows
    )
    if edit is not None:
        assert edit[0] in text, edit[0]
        text = text.replace(*edit)
    path = experiment["dir"] / "batches" / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_plan_refuses_a_retry_whose_original_was_never_planned(experiment: dict, capsys) -> None:
    path = _retry_spec(experiment, "t1r", "rows: {start: 2, stop: 2}")
    assert _plan(experiment, path) == 2
    assert "retry_of" in capsys.readouterr().err


def test_plan_refuses_a_retry_that_changes_a_paid_field(experiment: dict, capsys) -> None:
    assert _plan(experiment, Path(experiment["spec"])) == 0  # the original's record
    path = _retry_spec(
        experiment, "t1r", "rows: {start: 2, stop: 2}", ("budget_per_seat: 2000000", "budget_per_seat: 1000000")
    )
    assert _plan(experiment, path) == 2
    err = capsys.readouterr().err
    assert "budget_per_seat" in err


def test_plan_refuses_a_retry_outside_the_original_slice(experiment: dict, capsys) -> None:
    assert _plan(experiment, Path(experiment["spec"])) == 0
    path = _retry_spec(experiment, "t1r", "rows: {start: 3, stop: 3}")
    assert _plan(experiment, path) == 2
    assert "c__c-3" in capsys.readouterr().err


def test_plan_accepts_a_retry_of_one_row_of_the_original(experiment: dict, capsys) -> None:
    assert _plan(experiment, Path(experiment["spec"])) == 0
    path = _retry_spec(experiment, "t1r", "rows: {start: 2, stop: 2}")
    assert _plan(experiment, path) == 0
    assert "retry of t1" in capsys.readouterr().out


def _metrics(cell: Path, rows: list[dict]) -> None:
    cell.mkdir(parents=True, exist_ok=True)
    (cell / "metrics.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_retry_merge_takes_the_last_valid_attempt(tmp_path: Path) -> None:
    first = tmp_path / "b"
    _metrics(
        first,
        [
            {"instance_id": "a", "run_summary": {"status": "completed", "tokens": 10}},
            {"instance_id": "b", "run_summary": {"status": "failed", "reason": "APIError", "tokens": 1}},
            {"instance_id": "c", "run_summary": {"status": "failed", "reason": "APIError", "tokens": 2}},
            {"instance_id": "d", "run_summary": {"status": "completed", "tokens": 4}},
        ],
    )
    second = tmp_path / "b-r"
    _metrics(
        second,
        [
            {"instance_id": "b", "run_summary": {"status": "completed", "tokens": 90}},
            {"instance_id": "c", "run_summary": {"status": "failed", "reason": "APIError", "tokens": 3}},
            {"instance_id": "d", "run_summary": {"status": "completed", "tokens": 40}},
        ],
    )
    rows = cell_report.merge_attempts(
        [("b", cell_report.run_rows(first, "single")), ("b-r", cell_report.run_rows(second, "single"))]
    )
    by_id = {r.instance_id: r for r in rows}
    assert sorted(by_id) == ["a", "b", "c", "d"]
    assert (by_id["a"].attempt, by_id["a"].attempts, by_id["a"].source_batch) == (1, 1, "b")
    # b's second attempt is the one that ran: the failed first attempt is dropped.
    assert (by_id["b"].attempt, by_id["b"].attempts, by_id["b"].source_batch) == (2, 2, "b-r")
    assert by_id["b"].tokens == 90
    # c never ran: the last attempt is kept so the instance is not silently gone.
    assert (by_id["c"].attempt, by_id["c"].attempts, by_id["c"].source_batch) == (2, 2, "b-r")
    assert by_id["c"].tokens == 3 and not by_id["c"].valid
    # Both attempts at d ran. The rule is the LAST that ran, not the first.
    assert (by_id["d"].attempt, by_id["d"].attempts, by_id["d"].source_batch) == (2, 2, "b-r")
    assert by_id["d"].tokens == 40
    summary = cell_report.summarize(rows, team=False)
    assert summary["retried"] == ["b", "c", "d"] and summary["retried_count"] == 3
    assert summary["retry_succeeded"] == ["b", "d"] and summary["retry_succeeded_count"] == 2
    assert summary["infra_failed"] == ["c"] and summary["infra_failed_count"] == 1
    assert "attempt 2 of 2" in cell_report.render(rows, summary, [])


def test_a_report_without_a_retry_names_every_run_attempt_one(tmp_path: Path) -> None:
    cell = tmp_path / "b"
    _metrics(
        cell,
        [
            {"instance_id": "a", "run_summary": {"status": "completed", "tokens": 10}},
            {"instance_id": "b", "run_summary": {"status": "failed", "reason": "APIError", "tokens": 1}},
        ],
    )
    rows = cell_report.merge_attempts([("b", cell_report.run_rows(cell, "single"))])
    assert [(r.attempt, r.attempts, r.source_batch) for r in rows] == [(1, 1, "b"), (1, 1, "b")]
    summary = cell_report.summarize(rows, team=False)
    assert summary["retried"] == [] and summary["retry_succeeded"] == []
    # One attempt that never ran is still an instance the endpoint took away.
    assert summary["infra_failed"] == ["b"]
    assert "attempt" not in cell_report.render(rows, summary, [])
