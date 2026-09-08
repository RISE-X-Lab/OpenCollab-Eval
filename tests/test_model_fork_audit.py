"""The model-fork audit has to fail on the three things it claims to catch.

A checker that only ever says "fine" is indistinguishable from a checker that
is broken, so every gate below is tested from both sides: a model that really
does have a row in every table must pass, and a planted defect of each kind
must make the same command exit non-zero.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencollab_eval.experiment import model_fork_audit, model_fork_probe, model_forks
from opencollab_eval.experiment.model_forks import ForkResolutionError, OpenCollabSource

#: Has a row in every table on both sides. The positive control.
FULLY_LISTED = "deepseek-v4-flash"
#: Deliberately absent from OpenCollab's capability table (see the comment in
#: ``types.py``): its runs were produced under the unlisted-model fallback.
UNLISTED = "gpt-5.6-luna"

_SYMLINKED = (
    "adapters/llm/errors.py",
    "adapters/llm/retry.py",
    "adapters/llm/openai_provider.py",
    "adapters/llm/usage_ledger.py",
    "application/shaping/pipeline.py",
)


def forked_opencollab(tmp_path: Path, replacements: list[tuple[str, str]]) -> OpenCollabSource:
    """A checkout that is the real one apart from an edited ``types.py``.

    Everything except the edited file is symlinked, so the audit runs against
    the real classifiers and only the planted difference can move a result.
    """
    real = model_forks.OPENCOLLAB_PACKAGE
    root = tmp_path / "opencollab"
    for relative in ("adapters/llm", "application/shaping"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    for relative in _SYMLINKED:
        (root / relative).symlink_to(real / relative)
    text = (real / "adapters" / "llm" / "types.py").read_text(encoding="utf-8")
    for old, new in replacements:
        assert old in text, f"anchor no longer present in types.py: {old!r}"
        text = text.replace(old, new, 1)
    (root / "adapters" / "llm" / "types.py").write_text(text, encoding="utf-8")
    return OpenCollabSource(root)


def audit(*argv: str) -> tuple[int, str]:
    """Run the CLI the way a person would, returning ``(exit code, stdout)``."""
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = model_fork_audit.run(list(argv))
    return code, buffer.getvalue()


# --- the positive control -------------------------------------------------


def test_a_model_listed_in_every_table_passes() -> None:
    code, output = audit(FULLY_LISTED)
    assert code == 0, output
    assert "PASSED" in output
    assert "FAIL" not in output


def test_the_model_the_ladder_switched_to_passes_on_head() -> None:
    code, output = audit("qwen3.8-flash")
    assert code == 0, output


def test_the_shipped_declaration_matches_the_code_it_declares() -> None:
    declaration = Path("experiment/model-forks/qwen3.8-flash.yaml")
    code, output = audit("qwen3.8-flash", "--declare", str(declaration))
    assert code == 0, output


# --- failure 1: a silent fallback ----------------------------------------


def test_an_unlisted_model_is_reported_as_a_silent_fallback() -> None:
    code, output = audit(UNLISTED)
    assert code == 1
    assert "capabilities.row" in output
    for quantity in ("capabilities.context_window", "history.trigger", "history.target"):
        assert f"FAIL  {quantity}" in output, output
    # The fallback values themselves have to be named: "something defaulted" is
    # not actionable, "it ran on 120000/90000" is.
    assert "landed on 120000 by fallback" in output
    assert "landed on 90000 by fallback" in output


def test_the_fallback_gate_can_be_switched_off_on_its_own() -> None:
    code, output = audit(UNLISTED, "--no-fail-on-fallback")
    assert code == 0, output
    assert "note  capabilities.context_window" in output


def test_a_family_row_is_a_fallback_and_never_reads_as_exact() -> None:
    """A family row answers for a model nobody checked, and must still fail.

    ``deepseek-v9-preview`` has no row anywhere; OpenCollab's family table
    answers 64,000 for it because the name begins ``deepseek-``. That is an
    answer, not a checked one, so the gate has to fire on it exactly as it does
    on no answer at all.
    """
    findings = model_forks.resolve("deepseek-v9-preview")
    window = next(
        finding for finding in findings
        if finding.side == "opencollab" and finding.quantity == "capabilities.context_window"
    )
    assert window.hit == model_forks.FAMILY
    assert window.value == 64_000
    marks = model_fork_audit.verdicts(findings)
    assert marks[findings.index(window)][0] == model_fork_audit.FAIL


def test_the_two_repositories_do_not_match_families_by_the_same_rule() -> None:
    """``qwen3-max`` gets no window from OpenCollab and 131,072 from Eval, today.

    OpenCollab matches a family as the whole name or a prefix followed by a
    hyphen, so ``qwen3-max`` does not match ``qwen`` and history compaction
    would run on the fixed 120k/90k default. Eval matches the same table by
    *substring* (``usage.py``: ``if key in lowered``), so ``qwen`` does match and
    131,072 would be written into ``metrics.jsonl`` as the window the run had.
    Enforced and recorded differ, on a model nobody has run yet -- which is the
    point of checking before the batch rather than after it.
    """
    findings = model_forks.resolve("qwen3-max")
    resolved = {
        (finding.side, finding.quantity): finding
        for finding in findings
        if finding.quantity == "capabilities.context_window"
    }
    assert resolved[("opencollab", "capabilities.context_window")].value is None
    assert resolved[("eval", "capabilities.context_window")].value == 131_072
    assert resolved[("eval", "capabilities.context_window")].hit == model_forks.FAMILY
    compared = resolved[("cross-repo", "capabilities.context_window")]
    assert compared.hit == "DISAGREE"
    marks = model_fork_audit.verdicts(findings)
    assert marks[findings.index(compared)][0] == model_fork_audit.FAIL


def test_row_defaults_are_a_note_until_asked_for(tmp_path: Path) -> None:
    findings = model_forks.resolve(FULLY_LISTED)
    lenient = model_fork_audit.verdicts(findings)
    strict = model_fork_audit.verdicts(findings, fail_on_row_default=True)
    row_defaults = [
        index for index, finding in enumerate(findings) if finding.hit == model_forks.ROW_DEFAULT
    ]
    assert row_defaults, "deepseek's row leaves three Responses fields at their defaults"
    assert all(lenient[index][0] == model_fork_audit.NOTE for index in row_defaults)
    assert all(strict[index][0] == model_fork_audit.FAIL for index in row_defaults)


# --- failure 2: the two repositories disagree ----------------------------


def test_a_window_that_differs_between_the_repositories_fails(tmp_path: Path) -> None:
    """The exact defect of 2026-09-06: 131,072 enforced, 983,616 recorded."""
    source = forked_opencollab(
        tmp_path,
        [("        context_window=983_616,\n        supports_forced_tool_choice=False,",
          "        context_window=131_072,\n        supports_forced_tool_choice=False,")],
    )
    findings = model_forks.resolve("qwen3.8-flash", source=source)
    marks = model_fork_audit.verdicts(findings)
    disagreements = [
        model_fork_audit._key(finding)
        for index, finding in enumerate(findings)
        if marks[index][0] == model_fork_audit.FAIL and finding.side == "cross-repo"
    ]
    assert "cross-repo:capabilities.context_window" in disagreements
    assert "cross-repo:tables.shared_rows" in disagreements
    compared = next(
        finding for finding in findings
        if finding.side == "cross-repo" and finding.quantity == "capabilities.context_window"
    )
    assert compared.value == "oc=131072 eval=983616"


def test_the_cross_repo_gate_can_be_switched_off_on_its_own(tmp_path: Path) -> None:
    source = forked_opencollab(
        tmp_path,
        [("        context_window=983_616,\n        supports_forced_tool_choice=False,",
          "        context_window=131_072,\n        supports_forced_tool_choice=False,")],
    )
    findings = model_forks.resolve("qwen3.8-flash", source=source)
    marks = model_fork_audit.verdicts(findings, fail_on_cross_repo=False)
    assert not [
        index for index, finding in enumerate(findings)
        if finding.side == "cross-repo" and marks[index][0] == model_fork_audit.FAIL
    ]


def test_two_repositories_that_agree_report_no_drift() -> None:
    findings = model_forks.resolve(FULLY_LISTED)
    cross = [finding for finding in findings if finding.side == "cross-repo"]
    assert len(cross) == 3
    assert all(finding.hit == "agree" for finding in cross)


# --- failure 3: the declaration -----------------------------------------


def test_a_declared_value_the_code_does_not_have_fails(tmp_path: Path) -> None:
    declaration = tmp_path / "declared.json"
    declaration.write_text(json.dumps({"model": FULLY_LISTED, "expect": {"history.trigger": 999}}))
    code, output = audit(FULLY_LISTED, "--declare", str(declaration))
    assert code == 1
    assert "history.trigger: declared 999, resolved 1015576" in output


def test_a_declared_key_that_matches_no_quantity_fails(tmp_path: Path) -> None:
    """A typo in a declaration must not pass by checking nothing."""
    declaration = tmp_path / "declared.json"
    declaration.write_text(json.dumps({"expect": {"history.trigger_tokens": 1_015_576}}))
    code, output = audit(FULLY_LISTED, "--declare", str(declaration))
    assert code == 1
    assert "resolves no such quantity" in output


def test_the_declaration_gate_can_be_switched_off_on_its_own(tmp_path: Path) -> None:
    declaration = tmp_path / "declared.json"
    declaration.write_text(json.dumps({"expect": {"history.trigger": 999}}))
    code, output = audit(FULLY_LISTED, "--declare", str(declaration), "--no-fail-on-declaration")
    assert code == 0, output
    assert "declared 999" in output


def test_a_declaration_for_another_model_is_refused(tmp_path: Path) -> None:
    declaration = tmp_path / "declared.json"
    declaration.write_text(json.dumps({"model": "qwen3.8-flash", "expect": {}}))
    with pytest.raises(ForkResolutionError):
        audit(FULLY_LISTED, "--declare", str(declaration))


# --- the resolver's own positive control ---------------------------------


def test_the_resolver_stops_when_its_copy_of_the_rules_has_drifted() -> None:
    """The attribution is re-implemented, so it has to be checked against reality."""
    findings = model_forks.resolve(FULLY_LISTED)
    honest = {
        finding.quantity.split(".", 1)[1]: finding.value
        for finding in findings
        if finding.side == "opencollab" and finding.quantity.startswith("capabilities.")
    }
    honest.pop("row")
    model_forks._check_runtime_agreement(findings, {"capabilities": honest})
    drifted = dict(honest, context_window=1)
    with pytest.raises(ForkResolutionError, match="no longer matches OpenCollab"):
        model_forks._check_runtime_agreement(findings, {"capabilities": drifted})


def test_a_new_capability_field_is_not_silently_left_unresolved() -> None:
    findings = model_forks.resolve(FULLY_LISTED)
    runtime = {
        "capabilities": {
            finding.quantity.split(".", 1)[1]: finding.value
            for finding in findings
            if finding.side == "opencollab" and finding.quantity.startswith("capabilities.")
        }
    }
    runtime["capabilities"].pop("row")
    runtime["capabilities"]["supports_something_new"] = True
    with pytest.raises(ForkResolutionError, match="gained field"):
        model_forks._check_runtime_agreement(findings, runtime)


def test_a_missing_opencollab_file_is_an_error_not_an_empty_table(tmp_path: Path) -> None:
    with pytest.raises(ForkResolutionError, match="does not exist"):
        OpenCollabSource(tmp_path / "not-a-checkout")


# --- the replayed provider errors ----------------------------------------


def test_replayed_errors_answer_in_both_directions() -> None:
    findings = {
        finding.quantity: finding.value
        for finding in model_forks.resolve(FULLY_LISTED)
        if finding.quantity.startswith(("overflow.", "retry."))
    }
    assert findings["overflow.openai-max-context"] is True
    assert findings["overflow.plain-400"] is False
    assert findings["retry.http-429"] is True
    assert findings["retry.app-valueerror"] is False


def test_a_samples_file_replaces_the_replayed_errors(tmp_path: Path) -> None:
    samples = tmp_path / "samples.json"
    samples.write_text(json.dumps({
        "overflow": [{"label": "planted", "message": "prompt is too long", "status": 400}],
        "retry": [{"label": "planted", "message": "Service Unavailable", "status": 503}],
    }))
    code, output = audit(FULLY_LISTED, "--samples", str(samples), "--json")
    assert code == 0
    quantities = {row["quantity"] for row in json.loads(output)["findings"]}
    assert "overflow.planted" in quantities
    assert "overflow.plain-400" not in quantities


# --- the endpoint probes (no network: a fake sender) ---------------------


def _reply(text: str, *, status: int = 200, finish: str = "stop", tokens: int = 8) -> model_fork_probe.Response:
    return model_fork_probe.Response(
        status=status,
        body={
            "choices": [{"message": {"content": text}, "finish_reason": finish}],
            "usage": {"completion_tokens": tokens},
        },
    )


def test_the_context_probe_calls_a_dropped_head_a_failure() -> None:
    """A tail-only echo must never be scored as a window that held."""
    sent: list[dict] = []

    def sender(payload: dict) -> model_fork_probe.Response:
        sent.append(payload)
        tail = payload["messages"][0]["content"].split("TAIL-CODE ")[1].split("\n")[0]
        return _reply(tail)

    result = model_fork_probe.probe_context("m", sender, budget=3, ceiling_tokens=64_000)
    assert result["largest_input_answered_with_both_sentinels"] is None
    assert {attempt["verdict"] for attempt in result["attempts"]} == {"head-dropped"}
    assert len(sent) == 3


def test_the_context_probe_brackets_the_real_ceiling() -> None:
    ceiling = 40_000

    def sender(payload: dict) -> model_fork_probe.Response:
        content = payload["messages"][0]["content"]
        head = content.split("HEAD-CODE ")[1].split("\n")[0]
        tail = content.split("TAIL-CODE ")[1].split("\n")[0]
        if len(content.split()) > ceiling:
            return model_fork_probe.Response(status=400, error_text="Range of input length should be [1, 40000]")
        return _reply(f"{head} {tail}")

    result = model_fork_probe.probe_context("m", sender, budget=8, ceiling_tokens=64_000)
    largest, smallest = (
        result["largest_input_answered_with_both_sentinels"],
        result["smallest_input_that_failed"],
    )
    assert largest is not None and smallest is not None
    assert largest <= ceiling + 40 < smallest + 40
    assert result["requests_spent"] <= 8
    refused = [attempt for attempt in result["attempts"] if attempt["verdict"] == "refused"]
    assert refused and "Range of input length" in refused[0]["endpoint_said"]


def test_an_output_cut_is_not_reported_as_an_input_limit() -> None:
    def sender(payload: dict) -> model_fork_probe.Response:
        return _reply("I will now begin", finish="length")

    result = model_fork_probe.probe_context("m", sender, budget=1, ceiling_tokens=8_000)
    assert result["attempts"][0]["verdict"] == "output-truncated"
    assert result["attempts"][0]["finish_reason"] == "length"


def test_the_tool_choice_probe_keeps_the_endpoints_own_wording() -> None:
    def sender(payload: dict) -> model_fork_probe.Response:
        if payload["tool_choice"] == "auto":
            return model_fork_probe.Response(
                status=200,
                body={"choices": [{"message": {"tool_calls": [{"id": "1"}]}, "finish_reason": "tool_calls"}]},
            )
        return model_fork_probe.Response(
            status=400,
            error_text="The tool_choice parameter does not support being set to required in thinking mode",
        )

    result = model_fork_probe.probe_tool_choice("m", sender, budget=3)
    assert result["auto"]["http_status"] == 200 and result["auto"]["tool_calls"] == 1
    assert result["required"]["http_status"] == 400
    assert "thinking mode" in result["named"]["endpoint_said"]


def test_the_max_tokens_probe_sends_each_field_name_once() -> None:
    seen: list[str] = []

    def sender(payload: dict) -> model_fork_probe.Response:
        seen.extend(key for key in ("max_tokens", "max_completion_tokens") if key in payload)
        return _reply("1 2 3", finish="length", tokens=32)

    result = model_fork_probe.probe_max_tokens("m", sender, budget=2)
    assert seen == ["max_tokens", "max_completion_tokens"]
    assert result["max_tokens"]["completion_tokens"] == 32
    assert result["max_completion_tokens"]["finish_reason"] == "length"


def test_probes_never_exceed_the_request_ceiling() -> None:
    calls = 0

    def sender(payload: dict) -> model_fork_probe.Response:
        nonlocal calls
        calls += 1
        return _reply("nothing useful")

    report = model_fork_probe.run_probes(
        "m", requested="context,tool_choice,max_tokens", max_requests=6, sender=sender
    )
    assert calls <= 6
    assert report["results"]["tool_choice"]["auto"]["http_status"] == 200
    assert "max_tokens" in report["results"]


def test_a_dry_run_costs_nothing_and_sends_nothing() -> None:
    def sender(payload: dict) -> model_fork_probe.Response:  # pragma: no cover - must not run
        raise AssertionError("a dry run must not reach the endpoint")

    report = model_fork_probe.run_probes("m", requested="context", max_requests=4, dry_run=True, sender=sender)
    assert report["dry_run"] is True
    assert "results" not in report
    assert report["cost_estimate"]["requests"] == 3


def test_an_unknown_probe_name_is_refused_before_anything_is_sent() -> None:
    with pytest.raises(model_fork_probe.ProbeUnavailable, match="unknown probe"):
        model_fork_probe.run_probes("m", requested="contxt", max_requests=4, dry_run=True)


def test_a_probe_report_never_carries_the_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCOLLAB_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("OPENCOLLAB_API_KEY", "sk-planted-secret-value")

    def sender(payload: dict) -> model_fork_probe.Response:
        return _reply("ok")

    report = model_fork_probe.run_probes("m", requested="max_tokens", max_requests=2, sender=sender)
    assert "sk-planted-secret-value" not in json.dumps(report)


def test_a_missing_credential_stops_the_probe_rather_than_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("OPENCOLLAB_BASE_URL", "OPENCOLLAB_API_KEY", "OPENAI_API_KEY", "OPENCOLLAB_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(model_fork_probe.ProbeUnavailable):
        model_fork_probe.credentials()


# --- the tool is read-only -----------------------------------------------


def test_the_audit_writes_nothing_into_either_checkout() -> None:
    def snapshot(root: Path) -> dict[str, float]:
        return {
            str(path): path.stat().st_mtime
            for path in root.rglob("*.py")
            if "__pycache__" not in path.parts
        }

    before = snapshot(model_forks.OPENCOLLAB_PACKAGE)
    code, _ = audit(FULLY_LISTED)
    assert code == 0
    assert snapshot(model_forks.OPENCOLLAB_PACKAGE) == before
