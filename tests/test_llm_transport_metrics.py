"""Every arm records how it reached the provider, in the same four keys.

The workflow/team generator wrote ``wire_protocol``, ``reasoning_effort``,
``llm_base_url_sha256`` and ``workflow_env``; the single-agent generator wrote
none of them. Three of the arms a comparison is made between could therefore
say which protocol, which reasoning effort, which endpoint and which switches
they ran under, and one could not -- a difference in an input to every per-run
analysis, on an axis the arms are supposed to be identical on, and invisible in
the predictions themselves.

``OPENCOLLAB_LLM_STREAM_CHAT`` is the switch these tests care about most. The
reasoning body of a response cannot be retrieved from a non-streaming chat
completion, so a batch run with it off has paid for reasoning it did not keep;
until it was recorded, whether it had been on was not answerable from the run's
own record.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest

from opencollab_eval.generation import gen_prediction_config as config

gp = pytest.importorskip("opencollab_eval.generation.gen_prediction")

_SOURCE = Path(config.__file__).resolve().parent

#: Every module that assembles a prediction's metric record. One list, so a
#: generator added later is checked by every test below rather than by the ones
#: somebody remembered to extend.
GENERATOR_MODULES: tuple[str, ...] = (
    "gen_prediction",
    "gen_prediction_best_of_n",
    "gen_prediction_workflow",
)


# --------------------------------------------------------------------------
# the block itself


def test_the_stream_switch_is_one_of_the_recorded_environment_keys() -> None:
    # Deliberately pinned by name. It is the switch that decides whether the
    # reasoning body of a response can be retrieved at all, and it was the one
    # key missing from the recorded set.
    assert "OPENCOLLAB_LLM_STREAM_CHAT" in config.LLM_ENV_KEYS


def test_only_the_environment_keys_that_are_set_are_recorded(monkeypatch) -> None:
    # An absent switch and an empty one are different facts about the run.
    for key in config.LLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENCOLLAB_LLM_STREAM_CHAT", "true")

    assert config.observed_llm_env() == {"OPENCOLLAB_LLM_STREAM_CHAT": "true"}


def test_the_transport_block_reports_the_configuration_the_run_resolved(
    monkeypatch,
) -> None:
    for key in config.LLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENCOLLAB_WIRE_PROTOCOL", "responses")

    block = config.llm_transport_metrics(
        {
            "wire_protocol": "responses",
            "reasoning_effort": "high",
            "base_url_sha256": "f" * 64,
        }
    )

    assert set(block) == set(config.LLM_TRANSPORT_METRIC_KEYS)
    assert block["wire_protocol"] == "responses"
    assert block["reasoning_effort"] == "high"
    assert block["llm_base_url_sha256"] == "f" * 64
    assert block["workflow_env"] == {"OPENCOLLAB_WIRE_PROTOCOL": "responses"}


def test_a_run_with_no_wire_settings_still_names_its_protocol() -> None:
    # A missing key would read as "this arm does not record the protocol"; the
    # default is a fact about the run and is written as one.
    assert config.llm_transport_metrics({})["wire_protocol"] == "chat_completions"


# --------------------------------------------------------------------------
# both generators splice in the same block


def _module_source(name: str) -> str:
    return (_SOURCE / f"{name}.py").read_text(encoding="utf-8")


def test_no_generator_keeps_a_second_copy_of_the_environment_key_list() -> None:
    # The list drifted once already by being written out twice. A generator
    # that carries its own copy is the shape that lets one arm record a switch
    # the others do not.
    carriers = [
        name
        for name in GENERATOR_MODULES
        if "OPENCOLLAB_LLM_USER_AGENT" in _module_source(name)
    ]

    assert carriers == []


@pytest.mark.parametrize("module", GENERATOR_MODULES)
def test_every_generator_splices_the_shared_transport_block_into_its_metrics(
    module: str,
) -> None:
    tree = ast.parse(_module_source(module))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "llm_transport_metrics"
    ]

    assert calls, f"{module} builds its metrics without the shared transport block"


# --------------------------------------------------------------------------
# and the single-agent path writes them for real


@pytest.fixture
def _isolated_solver_snapshot(monkeypatch):
    monkeypatch.setattr(gp, "container_image_id", lambda cid: "sha256:" + "8" * 64)
    evidence = gp.SolverGitSnapshot(
        anonymous_head="a" * 40,
        base_tree="b" * 40,
        commit_count=1,
        remote_count=0,
        extra_git_metadata=0,
        removed_git_metadata=0,
    )
    monkeypatch.setattr(
        gp, "prepare_solver_git_snapshot", lambda cid, expected: evidence
    )
    baseline = type(
        "Baseline", (), {"snapshot": evidence, "cleanup": lambda self: None}
    )()
    monkeypatch.setattr(
        gp, "prepare_trusted_patch_baseline", lambda cid, snapshot: baseline
    )
    return evidence


def _trusted_extraction(patch: str):
    encoded = patch.encode("utf-8")
    return gp.gen_prediction_patch.TrustedPatchExtraction(
        fixed_anonymous_base="a" * 40,
        base_tree="b" * 40,
        baseline_archive_sha256="c" * 64,
        baseline_archive_bytes=10,
        baseline_archive_entries=1,
        baseline_extracted_bytes=1,
        workspace_archive_sha256="d" * 64,
        workspace_archive_bytes=10,
        workspace_archive_entries=1,
        workspace_extracted_bytes=1,
        patch_sha256=hashlib.sha256(encoded).hexdigest(),
        patch_bytes=len(encoded),
        candidate_tree="e" * 40,
        changed_paths=(),
        path_modes=(),
    )


def test_a_finished_single_agent_run_records_how_it_reached_the_provider(
    monkeypatch, tmp_path, _isolated_solver_snapshot
) -> None:
    instance = tmp_path / "instance.json"
    instance.write_text(
        json.dumps(
            {
                "instance_id": "task-1",
                "base_commit": "c" * 40,
                "repo": "acme/repo",
                "problem_statement": "fix it",
                "FAIL_TO_PASS": "[]",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "predictions.jsonl"
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    for key in config.LLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("OPENCOLLAB_EVAL_WORKFLOW_ENV", raising=False)
    monkeypatch.delenv("OPENCOLLAB_EVAL_LLM_BASE_URL_SHA256", raising=False)
    monkeypatch.setenv("OPENCOLLAB_LLM_STREAM_CHAT", "true")
    monkeypatch.setattr(
        gp,
        "get_config",
        lambda root: {
            "model": "model",
            "provider": "provider",
            "api_key": "key",
            "base_url": "http://local",
            "base_url_sha256": "a" * 64,
            "wire_protocol": "responses",
            "reasoning_effort": "high",
        },
    )
    monkeypatch.setattr(gp, "start_container", lambda image, name, token: "cid")

    async def fake_run_agent(*args, **kwargs):
        return {
            "workflow_status": "done",
            "session_quiesced": True,
            "execution_quiesced": True,
            "candidate_probe_eligible": True,
            "submission_eligible": True,
        }

    monkeypatch.setattr(gp, "run_agent", fake_run_agent)
    monkeypatch.setattr(gp, "require_container_quiescence", lambda cid: None)
    monkeypatch.setattr(
        gp, "run_with_bounded_shutdown", lambda awaitable: asyncio.run(awaitable)
    )
    monkeypatch.setattr(
        gp,
        "extract_patch_guarded",
        lambda cid, baseline: (patch, [], _trusted_extraction(patch).as_dict()),
    )
    monkeypatch.setattr(gp, "_remove_labeled_container", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gen_prediction.py",
            "--instance-file",
            str(instance),
            "--output",
            str(output),
        ],
    )

    gp.main()

    row = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    recorded = row["workflow_metric"]
    assert recorded["wire_protocol"] == "responses"
    assert recorded["reasoning_effort"] == "high"
    assert recorded["llm_base_url_sha256"] == "a" * 64
    assert recorded["workflow_env"] == {"OPENCOLLAB_LLM_STREAM_CHAT": "true"}
