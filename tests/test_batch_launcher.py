"""A batch file has to become the driver's exact command, and the pre-flight has to refuse."""

from __future__ import annotations

import hashlib
import json
import math
import shlex
import subprocess
import textwrap
from pathlib import Path

import pytest

from opencollab_eval.commands import batch as batch_cli
from opencollab_eval.experiment import batch_remote, batch_score, cell_report
from opencollab_eval.experiment.batch_spec import (
    RUNG_CELLS,
    SpecError,
    build_instances,
    card_file_paths,
    cell_team_file,
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


def test_a_spec_may_name_its_own_frame_content(experiment: dict, tmp_path: Path) -> None:
    """The host file says where the machine keeps its cache; the spec says which benchmark.

    Two benchmarks on one machine is the case this exists for. ``frame_content``
    is a field of ``HostConfig``, whose docstring calls itself machine facts
    rather than choices -- and the one other way to get a second frame content
    in, a second host file for the same machine differing in that field, is
    what the ``lthpc-*`` parity test exists to refuse.
    """
    other = tmp_path / "other-frame.jsonl"
    other.write_text(
        "".join(
            json.dumps(
                {"instance_id": i, "repo": r, "problem_statement": f"other {i}", "FAIL_TO_PASS": "[]"},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
            for i, r in (("a__a-1", "a/a"), ("b__b-2", "b/b"), ("c__c-3", "c/c"))
        ),
        encoding="utf-8",
    )
    path = experiment["dir"] / "batches" / "o.yaml"
    path.write_text(_spec_text(experiment, "pins:", f"frame_content: {other}\npins:"), encoding="utf-8")
    batch = batch_cli.Batch(path, experiment["dir"], experiment["dir"] / "hosts" / "h.yaml")
    assert batch.frame_content == str(other)
    assert "other a__a-1" in batch.instances_text


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


def test_a_cell_outside_the_ladder_names_its_own_team_file() -> None:
    """A later family files its team configs apart from the ladder's namespace.

    The first assertion is the control and the load-bearing one: every cell the
    paper already reports must resolve to the path it resolved to before this
    function existed, or the change is a silent renaming of every measured
    batch. The second is the reason it exists -- OpenCollab's
    ``tests/test_analyst_card_assembly.py`` globs ``configs/team.handoff.*.yaml``
    and reads the hits as one instrument, so a second roster cannot be filed
    there.
    """
    assert cell_team_file("cmd-plain") == "configs/team.handoff.cmd-plain.yaml"
    assert cell_team_file("facts-v2") == "configs/team.handoff.facts-v2.yaml"
    assert cell_team_file("dual-bare") == "configs/team.dual-bare.yaml"
    assert cell_team_file("dual-judge") == "configs/team.dual-judge.yaml"
    assert cell_team_file("s2dual-judge") == "configs/team.s2dual-judge.yaml"
    # The s2dual roster with the Adopter's tools changed is a family of its
    # own in OpenCollab (``configs/s2tools``), filed apart from both.
    assert cell_team_file("s2tools-adopt") == "configs/team.s2tools-adopt.yaml"


@pytest.mark.parametrize("cell", ["x", "dual-x"])
def test_every_call_site_agrees_on_where_a_cell_is_seated(experiment: dict, cell: str) -> None:
    """Three places name a cell's team file, and they are one path.

    They were three separate format strings, and the ``dual-x`` case is what
    tells them apart -- a ladder cell resolves the same either way. Two of the
    three were found by reading; the third, the path pre-flight asks the host
    to digest the role prompts from, was found only when pre-flight refused a
    real batch. Enumerating them here is what keeps the next one from being
    found that way again.
    """
    repo = Path(experiment["repo"])
    (repo / "configs" / f"team.{cell}.yaml").write_text(
        "entry: analyst\nroles:\n  analyst: {prompt_file: handoff-experiment/analyst.x.md, tools: [bash]}\n",
        encoding="utf-8",
    )
    path = experiment["dir"] / "batches" / "seat.yaml"
    path.write_text(_spec_text(experiment, "cell: x", f"cell: {cell}"), encoding="utf-8")
    spec = load_spec(path)
    host = load_host(experiment["dir"] / "hosts" / "h.yaml")
    rel = card_file_paths(spec, repo)[0]

    # 1. the bytes pre-flight compares, 2. the argv the driver is launched with,
    # 3. the file pre-flight asks the host to read the role prompts out of.
    assert rel == cell_team_file(cell)
    assert spec.team_config_relpath(host) == f"{host.opencollab_dir}/{rel}"
    script = batch_remote.preflight_script(spec, host, [rel], ["img/a-1:latest"])
    assert f"{host.workdir}/{host.opencollab_dir}/{rel}" in script
    assert "team.handoff." not in script or not cell.startswith("dual-")


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
        (("ENDPOINT\t200", "ENDPOINT\t502"), "endpoint answers a completion"),
        (("ENDPOINT\t200", "ENDPOINT\t401"), "endpoint answers a completion"),
        (("ENDPOINT\t200", "MODEL_ENV\tpresent"), "endpoint answers a completion"),
        (("REMOTE_INSTANCES_SHA\tabsent", "REMOTE_INSTANCES_SHA\tzzz"), "instance file on host"),
    ],
)
def test_preflight_fails_on_each_drift(experiment: dict, mutation: tuple[str, str], failing: str) -> None:
    facts = _facts(experiment).replace(*mutation)
    checks = _checks(experiment, facts)
    assert not checks[failing].ok, checks[failing]


def test_preflight_fails_when_a_seat_is_given_something_other_than_the_pinned_prompt(
    experiment: dict,
) -> None:
    """A seat filled from a file the record does not name is a batch nothing can reproduce.

    The host is asked for one digest per seat. This replaces one of them with
    the hash of some other text, leaving the card-byte checks untouched -- the
    file on disk is still the pinned one, it is just not what reached the seat.
    """
    facts = _facts(experiment)
    spec = load_spec(experiment["spec"])
    real = batch_cli.blob_sha256(experiment["repo"], experiment["sha"], "configs/handoff-experiment/coder.md")
    swapped = facts.replace(
        f'"coder": "{real}"', f'"coder": "{hashlib.sha256(b"a prompt from somewhere else").hexdigest()}"'
    )
    assert swapped != facts, "the fixture must carry the real digest for this to test anything"
    checks = _checks(experiment, swapped)
    assert not checks["role prompt digests computed on host"].ok
    assert checks["card bytes configs/handoff-experiment/coder.md"].ok
    assert spec.cell == "x"


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


def test_the_endpoint_probe_keeps_the_key_out_of_the_process_table(experiment: dict) -> None:
    """Seventeen accounts share this machine, so argv is public.

    The probe needs the key, which is why it is not a line in
    ``preflight_script`` -- that script is banned from reading it. What it must
    not do is hand it to a command: python opens the env file itself and builds
    the header in memory, so the only key-bearing thing on the command line is
    the file's path.
    """
    host = load_host(experiment["dir"] / "hosts" / "h.yaml")
    script = batch_remote.endpoint_probe_script(host, "configs/.env.x")
    assert "curl" not in script
    assert "sk-" not in script
    # Shell-quoting rewrites every apostrophe, so match on the quote-free parts.
    assert "OPENCOLLAB_API_KEY" in script and "Authorization" in script and "Bearer " in script
    # The env file's path appears twice: the -f test and the argv. Nothing else
    # on the command line comes from inside the file.
    assert script.count("configs/.env.x") == 2
    assert "/chat/completions" in script and "max_tokens" in script


@pytest.mark.parametrize(("kind", "answer"), [("file", "present"), ("symlink", "symlink")])
def test_preflight_tells_a_symlinked_model_env_from_a_file(
    experiment: dict, tmp_path: Path, kind: str, answer: str
) -> None:
    """OpenCollab's loader refuses a symlinked env file, so pre-flight must not call one present.

    On 09-23 a second checkout got its key files as symlinks: pre-flight said
    "model env file present" (``[ -f ]`` follows the link) and every run died in
    0.8 s on `config env path is not a regular file`. The real script runs here,
    because the distinction is made in the shell on the host.
    """
    import dataclasses

    spec = load_spec(experiment["spec"])
    host = dataclasses.replace(load_host(experiment["dir"] / "hosts" / "h.yaml"), workdir=str(tmp_path / "w"))
    env_path = Path(host.workdir) / host.opencollab_dir / spec.model_env
    env_path.parent.mkdir(parents=True)
    real = tmp_path / "real.env"
    real.write_text("OPENCOLLAB_MODEL=m\n", encoding="utf-8")
    if kind == "symlink":
        env_path.symlink_to(real)
    else:
        env_path.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")

    out = subprocess.run(
        ["bash", "-s"], input=batch_remote.preflight_script(spec, host, [], []),
        capture_output=True, text=True, timeout=120,
    ).stdout

    assert batch_remote.fact(batch_remote.parse_facts(out), "MODEL_ENV") == answer


def test_preflight_says_what_to_do_about_a_symlinked_model_env(experiment: dict) -> None:
    check = _checks(experiment, _facts(experiment).replace("MODEL_ENV\tpresent", "MODEL_ENV\tsymlink"))[
        "model env file present"
    ]
    assert not check.ok
    assert "symlink" in check.detail and "ln" in check.detail


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
    spec = load_spec(experiment["spec"])
    assert record["host"]["role_prompt_sha256"] == {
        Path(rel).name.split(".")[0]: batch_cli.blob_sha256(experiment["repo"], experiment["sha"], rel)
        for rel in card_file_paths(spec, experiment["repo"])
        if not rel.endswith(".yaml")
    }
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
    return {"work": work, "sha": sha, "eval_sha": eval_sha, "host_file": host_file, "repo": repo,
            "seat_digests": seat_digests}


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
    out += _bash(batch_remote.endpoint_probe_script(host, spec.model_env))
    assert "sk-verysecret" not in out
    facts = batch_remote.parse_facts(out)
    checks = {c.name: c for c in batch_remote.evaluate_preflight(spec, host, facts, cards, "abc")}
    failed = [(c.name, c.detail) for c in checks.values() if not c.ok]
    assert failed == [], out
    record = batch_remote.facts_to_record(facts)
    assert record["model"] == "fake-model" and record["provider"] == "openai"
    assert record["base_url_sha256"] == hashlib.sha256(b"https://x.example/v1").hexdigest()
    assert record["card_sha256"] == cards
    assert record["role_prompt_sha256"] == fake_host["seat_digests"]


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
        "verify": "cmd-verify",
        "propose": "cmd-propose",
        "propose2": "cmd-propose2",
    }


def test_the_second_lthpc_checkout_is_the_same_machine() -> None:
    """Every `lthpc-*.yaml` may differ from `lthpc.yaml` in the checkout paths only.

    It exists so a second pin can run while a batch holds the first checkout,
    and it is only sound while every other field is identical: a second host
    file that quietly carried a different scoring dataset, python or workdir
    would turn "which card" into "which machine" without saying so.
    """
    import yaml

    a = yaml.safe_load((EXPERIMENT / "hosts" / "lthpc.yaml").read_text(encoding="utf-8"))
    others = sorted((EXPERIMENT / "hosts").glob("lthpc-*.yaml"))
    assert others, "the extra checkouts are what this test exists for"
    seen = set()
    for path in others:
        b = yaml.safe_load(path.read_text(encoding="utf-8"))
        differ = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
        assert differ == {"name", "opencollab_dir", "eval_dir"}, (path.name, differ)
        pair = (b["opencollab_dir"], b["eval_dir"])
        assert pair not in seen and pair != (a["opencollab_dir"], a["eval_dir"]), path.name
        seen.add(pair)


def test_checked_in_specs_name_rung_and_cell_consistently() -> None:
    # A ``TEMPLATE-`` file is not a spec: it carries `<family>`-style
    # placeholders on purpose, so ``load_spec`` refuses its name and the refusal
    # is the file working as intended. Loading it here turned this gate red for
    # every checked-in spec at once, which is how a real breakage would have
    # hidden.
    for path in sorted((EXPERIMENT / "batches").glob("*.yaml")):
        if path.name.startswith("TEMPLATE-"):
            continue
        spec = load_spec(path)
        if spec.arm == "team":
            assert spec.cell is not None, f"{path.name}: a team spec names the card it seats"
            # A rung is a name the paper reports a number under, and only the
            # cells in RUNG_CELLS have one. A batch on a ladder cell must say
            # which rung it is, or its number cannot be placed; a batch on a
            # roster outside the ladder must not, because naming one would file
            # its number under a rung it is not comparable with.
            if spec.cell in set(RUNG_CELLS.values()):
                assert spec.rung is not None, f"{path.name}: a ladder cell names its rung"
            else:
                assert spec.rung is None, f"{path.name}: {spec.cell} is not a cell of the ladder"
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
        f"EV_DIRTY\t{dirty}\n"
        + (f"RUNNING\t{running}\n" if running else "")
        + f"DECOY_HIT\t{decoy}\n"
    )


def _synced(experiment: dict) -> str:
    return f"OC_AFTER\t{experiment['sha']}\nEV_AFTER\t{PIN_EVAL}\n"


def test_sync_checks_out_both_repositories_at_the_spec_pins(experiment: dict, capsys) -> None:
    remote = SyncRemote(_guard(), _synced(experiment))

    assert _sync(experiment, remote) == 0

    out = capsys.readouterr().out
    assert "RESULT: synced" in out
    checkout = remote.scripts[1]
    assert experiment["sha"] in checkout and PIN_EVAL in checkout
    # Fetch only when the commit is not already there, so a repeat sync is free.
    assert 'cat-file -e "$sha^{commit}"' in checkout


def test_sync_refuses_while_a_driver_is_alive(experiment: dict, capsys) -> None:
    """A checkout would move the code under a batch that is still running.

    Nothing may be written, so the assertion is on the scripts sent: the guard
    and nothing else.
    """
    remote = SyncRemote(_guard(running="4242 python -m ...gen_prediction_batch --out-dir t1"))

    assert _sync(experiment, remote) == 1

    out = capsys.readouterr().out
    assert "REFUSED" in out and "driver(s) alive" in out
    assert len(remote.scripts) == 1
    assert "sync_repo()" not in remote.scripts[0]


def test_sync_refuses_when_its_own_driver_check_finds_no_decoy(experiment: dict, capsys) -> None:
    """A quiet result from a check that matches nothing is not evidence.

    The guard plants a process whose command line contains the pattern. If that
    is not found, "no driver is running" carries no information and the sync
    must not proceed on it.
    """
    remote = SyncRemote(_guard(decoy="0"))

    assert _sync(experiment, remote) == 1

    out = capsys.readouterr().out
    assert "REFUSED" in out and "decoy" in out
    assert len(remote.scripts) == 1


def test_sync_refuses_on_a_modified_tree(experiment: dict, capsys) -> None:
    remote = SyncRemote(_guard(dirty="3"))

    assert _sync(experiment, remote) == 1

    out = capsys.readouterr().out
    assert "REFUSED" in out and "modified tracked files" in out
    assert len(remote.scripts) == 1


def test_sync_reports_a_checkout_that_did_not_land(experiment: dict, capsys) -> None:
    """The host answering with some other sha is a failure, not a success."""
    remote = SyncRemote(_guard(), "OC_AFTER\tdeadbeef\nEV_AFTER\tdeadbeef\n")

    assert _sync(experiment, remote) == 1
    assert "RESULT: NOT synced" in capsys.readouterr().out


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
            ["bash", "-s"], input=batch_remote.sync_guard_script(host),
            capture_output=True, text=True, timeout=60, check=True,
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


@_NEEDS_PROC
def test_sync_guard_counts_a_driver_only_against_its_own_checkout(tmp_path: Path) -> None:
    """A batch running from `lthpc` must not stop a sync of `lthpc-b`, and must stop one of `lthpc`.

    The second checkout exists so a second pin can run while a batch holds the
    first; a guard that counts every driver on the machine refuses that sync
    whenever anything runs anywhere, which is the one situation it exists for.
    """
    from_a = _checkout_pythonpath(tmp_path, "lthpc.yaml")

    own = _guard_facts_with_a_driver(tmp_path, from_a, "lthpc.yaml")
    other = _guard_facts_with_a_driver(tmp_path, from_a, "lthpc-b.yaml")

    assert [f[0] for f in own] == ["RUNNING"]
    assert [f[0] for f in other] == ["ELSEWHERE"]


@_NEEDS_PROC
def test_sync_guard_counts_a_driver_it_cannot_attribute_against_every_checkout(tmp_path: Path) -> None:
    """No PYTHONPATH to read means no evidence the driver is elsewhere: refuse, as before."""
    facts = _guard_facts_with_a_driver(tmp_path, None, "lthpc-b.yaml")

    assert [f[0] for f in facts] == ["RUNNING"]


def test_sync_proceeds_past_drivers_on_other_checkouts(experiment: dict, capsys) -> None:
    guard = _guard() + "ELSEWHERE\t4242 python -m ...gen_prediction_batch --out-dir t1\n"
    remote = SyncRemote(guard, _synced(experiment))

    assert _sync(experiment, remote) == 0

    out = capsys.readouterr().out
    assert "RESULT: synced" in out
    assert "1 driver(s) on other checkouts" in out


def test_go_does_not_launch_when_sync_refuses(experiment: dict, capsys) -> None:
    remote = SyncRemote(_guard(running="4242 python -m ...gen_prediction_batch"))

    assert _sync(experiment, remote, command="go") == 1

    out = capsys.readouterr().out
    assert "RESULT: not launched" in out
    assert all("setsid nohup env" not in s for s in remote.scripts)


def test_a_fetch_retry_names_the_proxy_the_host_resolved(experiment: dict, capsys) -> None:
    """A fetch that cannot reach GitHub must say what route it tried.

    Both checkouts on lthpc carried a repository-local ``http.proxy =`` whose
    value was the empty string. It overrides the global setting, and
    ``config --get`` returns it with a zero exit status, so the sync exported
    no proxy, fetched direct, and reported three retries and a missing commit
    without naming the cause. The retry line now carries the resolved proxy,
    and "none" is one of the answers it can carry.
    """
    remote = SyncRemote(
        _guard(),
        "EV_PROXY\tnone\nEV_FETCH_RETRY\t1\nEV_MISSING\t" + PIN_EVAL + "\n"
        f"OC_PROXY\thttp://127.0.0.1:17890\nOC_AFTER\t{experiment['sha']}\n",
    )

    assert _sync(experiment, remote) == 1

    out = capsys.readouterr().out
    assert "fetch retry 1 (proxy none)" in out


def test_the_sync_script_falls_back_to_the_global_proxy(experiment: dict) -> None:
    """The repository-local value is read first, the global one when it is empty."""
    remote = SyncRemote(_guard(), _synced(experiment))

    assert _sync(experiment, remote) == 0

    checkout = remote.scripts[1]
    assert 'git -C "$d" config --get http.proxy' in checkout
    assert 'git config --global --get http.proxy' in checkout
    # The printf format carries a real tab and newline, as every fact line in
    # this script does, so the assertion is on the parts either side of them.
    assert '"%s_PROXY' in checkout and '"$tag" "${P:-none}"' in checkout


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


def _score_guard(*, preds: str = "present", rows: str = "2", empty: str = "0",
                 swebench: str = "5.0.2", disk: str = "500", decoy: str = "1",
                 running: str = "") -> str:
    return (
        f"PREDS\t{preds}\nPREDS_ROWS\t{rows}\nPREDS_EMPTY\t{empty}\n"
        f"SWEBENCH\t{swebench}\nSCORE_DIR\tabsent\n"
        + (f"RUNNING\t{running}\n" if running else "")
        + f"DECOY_HIT\t{decoy}\nDISK_FREE_GB\t{disk}\n"
    )


def _with_scoring_dataset(experiment: dict) -> None:
    host = Path(experiment["dir"]) / "hosts" / "h.yaml"
    host.write_text(
        host.read_text(encoding="utf-8") + "scoring_dataset: /home/u/ds.jsonl\n", encoding="utf-8"
    )


def _score(experiment: dict, remote: ScoreRemote, *extra: str) -> int:
    return batch_cli.main(
        ["--experiment-dir", str(experiment["dir"]), "score", str(experiment["spec"]), *extra],
        remote_factory=lambda h: remote,
    )


def test_score_refuses_when_the_host_names_no_dataset(experiment: dict, capsys) -> None:
    """Without a dataset there is nothing to score against, and no guess is safe."""
    remote = ScoreRemote(_score_guard())

    assert _score(experiment, remote) == 1

    assert "no scoring_dataset" in capsys.readouterr().out
    assert remote.scripts == []


def test_score_refuses_when_its_own_running_check_finds_no_decoy(experiment: dict, capsys) -> None:
    _with_scoring_dataset(experiment)
    remote = ScoreRemote(_score_guard(decoy="0"))

    assert _score(experiment, remote) == 1

    assert "decoy" in capsys.readouterr().out
    assert len(remote.scripts) == 1


def test_score_refuses_while_a_harness_is_already_running(experiment: dict, capsys) -> None:
    _with_scoring_dataset(experiment)
    remote = ScoreRemote(_score_guard(running="991 python -m swebench.harness.run_evaluation --run_id x"))

    assert _score(experiment, remote) == 1

    assert "already running" in capsys.readouterr().out
    assert len(remote.scripts) == 1


def test_score_refuses_a_predictions_file_shorter_than_the_batch(experiment: dict, capsys) -> None:
    """A short predictions file scores without complaint and returns a rate
    over the runs that happened to finish. The count is read first."""
    _with_scoring_dataset(experiment)
    remote = ScoreRemote(_score_guard(rows="1"))

    assert _score(experiment, remote) == 1

    out = capsys.readouterr().out
    assert "1 rows of 2 instances" in out and "RESULT: not scored" in out
    assert len(remote.scripts) == 1


def test_score_refuses_when_the_dataset_has_no_row_for_an_instance(experiment: dict, capsys) -> None:
    _with_scoring_dataset(experiment)
    remote = ScoreRemote(_score_guard(), dataset="DATASET_MISSING\tb__b-2\n")

    assert _score(experiment, remote) == 1

    assert "no row for b__b-2" in capsys.readouterr().out


def test_a_gold_run_that_leaves_a_task_unresolved_makes_the_session_unreadable() -> None:
    ok, detail = batch_score.gold_verdict(
        {"total": 51, "resolved": 50, "resolved_ids": [], "unresolved_ids": ["pylint-dev__pylint-4661"],
         "error_ids": [], "empty_patch_ids": [], "submitted": 51, "completed": 51}
    )
    assert not ok
    assert "pylint-dev__pylint-4661" in detail


def test_a_gold_run_that_scored_nothing_is_not_a_pass() -> None:
    """Zero of zero is the shape a missing report has, and it must not read as success."""
    ok, detail = batch_score.gold_verdict(
        {"total": 0, "resolved": 0, "resolved_ids": [], "unresolved_ids": [], "error_ids": [],
         "empty_patch_ids": [], "submitted": 0, "completed": 0}
    )
    assert not ok and "scored no instances" in detail


def test_the_gold_predictions_are_the_reference_patches() -> None:
    rows = [{"instance_id": "a__a-1", "patch": "diff --git a b"}]
    assert batch_score.gold_predictions(rows) == [
        {"instance_id": "a__a-1", "model_name_or_path": "gold", "model_patch": "diff --git a b"}
    ]


# --- derived specs ----------------------------------------------------------------


def test_derived_spec_is_refused_by_launch_and_copies_nothing(experiment: dict, capsys) -> None:
    """A spec over assembled predictions must not be launchable.

    Its out-dir holds a predictions file built from a batch that already ran,
    and every other command treats that directory like a batch's own. Launching
    into it would pay for runs that overwrite the artifact being read.
    """
    path = experiment["dir"] / "batches" / "derived.yaml"
    path.write_text(_spec_text(experiment, "name: t1", "name: t1\nderived: true"), encoding="utf-8")
    remote = FakeRemote(_facts(experiment))
    rc = batch_cli.main(
        ["--experiment-dir", str(experiment["dir"]), "launch", str(path)],
        remote_factory=lambda h: remote,
    )
    assert rc == 1
    assert remote.copied == []
    assert not any("setsid nohup env" in s for s in remote.scripts)
    assert "derived spec" in capsys.readouterr().out


def test_derived_does_not_move_the_digest(experiment: dict) -> None:
    """Adding the field must not change the identity of any batch already launched."""
    plain = load_spec(experiment["spec"])
    path = experiment["dir"] / "batches" / "derived2.yaml"
    path.write_text(_spec_text(experiment, "name: t1", "name: t1\nderived: true"), encoding="utf-8")
    assert spec_digest(load_spec(path)) == spec_digest(plain)


def test_derived_cannot_also_be_a_retry(experiment: dict) -> None:
    path = experiment["dir"] / "batches" / "derived3.yaml"
    path.write_text(
        _spec_text(experiment, "name: t1", "name: t1\nderived: true\nretry_of: t0"), encoding="utf-8"
    )
    with pytest.raises(SpecError, match="cannot also be a retry"):
        load_spec(path)


def test_derived_must_be_a_boolean(experiment: dict) -> None:
    path = experiment["dir"] / "batches" / "derived4.yaml"
    path.write_text(_spec_text(experiment, "name: t1", 'name: t1\nderived: "yes"'), encoding="utf-8")
    with pytest.raises(SpecError, match="must be true or false"):
        load_spec(path)
