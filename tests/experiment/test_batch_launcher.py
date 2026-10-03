"""Offline behavior checks for research batches and cell reports."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab_eval.commands import batch as batch_cli
from opencollab_eval.experiment import batch_remote, cell_report
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
from tests.experiment.batch_support import (
    _NEEDS_PROC as _NEEDS_PROC,
)
from tests.experiment.batch_support import (
    EXPERIMENT as EXPERIMENT,
)
from tests.experiment.batch_support import (
    HAND_LAUNCHED_ARGV as HAND_LAUNCHED_ARGV,
)
from tests.experiment.batch_support import (
    HAND_LAUNCHED_INSTANCES_SHA256 as HAND_LAUNCHED_INSTANCES_SHA256,
)
from tests.experiment.batch_support import (
    PIN_EVAL as PIN_EVAL,
)
from tests.experiment.batch_support import (
    PIN_OC as PIN_OC,
)
from tests.experiment.batch_support import (
    REAL_CACHE as REAL_CACHE,
)
from tests.experiment.batch_support import (
    ROOT as ROOT,
)
from tests.experiment.batch_support import (
    FakeRemote as FakeRemote,
)
from tests.experiment.batch_support import (
    ScoreRemote as ScoreRemote,
)
from tests.experiment.batch_support import (
    SyncRemote as SyncRemote,
)
from tests.experiment.batch_support import (
    _agent_file as _agent_file,
)
from tests.experiment.batch_support import (
    _bash as _bash,
)
from tests.experiment.batch_support import (
    _checkout_pythonpath as _checkout_pythonpath,
)
from tests.experiment.batch_support import (
    _checks as _checks,
)
from tests.experiment.batch_support import (
    _facts as _facts,
)
from tests.experiment.batch_support import (
    _git as _git,
)
from tests.experiment.batch_support import (
    _good_facts as _good_facts,
)
from tests.experiment.batch_support import (
    _guard as _guard,
)
from tests.experiment.batch_support import (
    _guard_facts_with_a_driver as _guard_facts_with_a_driver,
)
from tests.experiment.batch_support import (
    _score as _score,
)
from tests.experiment.batch_support import (
    _score_guard as _score_guard,
)
from tests.experiment.batch_support import (
    _spec_for_fake_host as _spec_for_fake_host,
)
from tests.experiment.batch_support import (
    _spec_text as _spec_text,
)
from tests.experiment.batch_support import (
    _sync as _sync,
)
from tests.experiment.batch_support import (
    _synced as _synced,
)
from tests.experiment.batch_support import (
    _with_scoring_dataset as _with_scoring_dataset,
)
from tests.experiment.batch_support import (
    experiment as experiment,
)
from tests.experiment.batch_support import (
    fake_host as fake_host,
)
from tests.experiment.batch_support import (
    oc_repo as oc_repo,
)


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
    path.write_text(_spec_text(experiment, experiment["eval_sha"], experiment["eval_sha"][:7]), encoding="utf-8")
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


@pytest.mark.skipif(not REAL_CACHE.exists(), reason="frame content cache not built on this machine")
def test_real_spec_reproduces_the_hand_built_instance_file() -> None:
    from opencollab_eval.experiment.batch_spec import load_frame_content, sha256_text

    spec = load_spec(EXPERIMENT / "batches" / "cmdplain30.yaml")
    rows = suite_rows(spec, EXPERIMENT / "suite")
    text = build_instances(rows, load_frame_content(REAL_CACHE))
    assert sha256_text(text) == HAND_LAUNCHED_INSTANCES_SHA256


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
    assert env["https_proxy"] == "http://proxy.example:8888"
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
    # The handoff roles seated as Single2 over topologies with edges removed
    # (``configs/s2sc``) are a third family, filed apart the same way.
    assert cell_team_file("s2sc-judge-bypass") == "configs/team.s2sc-judge-bypass.yaml"
    assert cell_team_file("s2sc-judge-pipeline") == "configs/team.s2sc-judge-pipeline.yaml"


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
    """The generated command passes a path; the SDK reads credentials in memory."""
    host = load_host(experiment["dir"] / "hosts" / "h.yaml")
    script = batch_remote.endpoint_probe_script(host, "configs/.env.x")
    assert "curl" not in script
    assert "sk-" not in script
    # Shell-quoting rewrites every apostrophe, so match on the quote-free parts.
    assert "create_model_client" in script and "OpenCollab" in script
    assert "OPENCOLLAB_API_KEY" not in script
    # The path is supplied through the same environment as the driver.
    assert script.count("configs/.env.x") == 1
    assert "max_output_tokens" in script and "llm_max_retries" in script


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
    host = dataclasses.replace(
        load_host(experiment["dir"] / "hosts" / "h.yaml"),
        workdir=str(tmp_path / "w"),
        python=sys.executable,
    )
    host = SimpleNamespace(**vars(host), pythonpath=os.environ.get("PYTHONPATH", ""))
    Path(host.workdir).mkdir()
    env_path = Path(host.workdir) / host.opencollab_dir / spec.model_env
    env_path.parent.mkdir(parents=True)
    real = tmp_path / "real.env"
    real.write_text("OPENCOLLAB_MODEL=m\n", encoding="utf-8")
    if kind == "symlink":
        env_path.symlink_to(real)
    else:
        env_path.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")

    out = subprocess.run(
        ["bash", "-s"],
        input=batch_remote.preflight_script(spec, host, [], []),
        capture_output=True,
        text=True,
        timeout=120,
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
    assert "OpenCollab" in script and "configuration" in script
    assert "base_url_sha256" in script and "OpenCollab" in script
    assert "[o]pencollab_eval.generation.gen_prediction_batch" in script


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
    assert cell_report.clopper_pearson(0, 0) == (None, None)  # no denominator, no interval


def test_decoy_carries_the_unbracketed_pattern() -> None:
    from opencollab_eval.experiment.batch_spec import BATCH_PROCESS_PATTERN

    plain = BATCH_PROCESS_PATTERN.replace("[o]", "o")
    assert plain in batch_remote.DECOY_MARK


@_NEEDS_PROC
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
    path.write_text(_spec_text(experiment, experiment["eval_sha"], "abcdef0123" * 4), encoding="utf-8")
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
