"""Tests for scan_batch.py.

Design note: the mixed fixture cell is built so that the *invalid* run
``acme__acme-2`` also calls ``message_agent``.  A scanner that lets dead runs
into alpha therefore reports 2/5 instead of 1/1 -- both numerator and
denominator move -- so these assertions actually discriminate rather than
happening to pass.  Each mutation in ``mutate.py`` flips at least one of them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, SCRIPTS)

import scan_batch  # noqa: E402

MIXED = os.path.join(FIXTURES, "cell-mixed")
CLEAN = os.path.join(FIXTURES, "cell-clean")
UNKNOWN = os.path.join(FIXTURES, "cell-unknown")
ALPHA = scan_batch.ALPHA_VALID_CLASSES
OUTCOME = scan_batch.OUTCOME_VALID_CLASSES


# --------------------------------------------------------------------------
# 1. The classifier, on the five shapes taken from the writer-side source.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,reason,error,expected",
    [
        ("completed", None, None, "completed"),
        ("failed", "APIError: Upstream response stream was interrupted", None, "api_error"),
        ("failed", None, "Upstream response stream was interrupted", "api_error"),
        ("failed", "ResponsesStreamInterruptedError", None, "api_error"),
        # These four carry no "upstream"/"provider"/"stream" wording, so only the
        # openai-SDK class-name branch of the api_error rule can match them.
        ("failed", "APIStatusError", None, "api_error"),
        ("failed", "APIConnectionError", None, "api_error"),
        ("failed", "APITimeoutError", None, "api_error"),
        ("failed", "RateLimitError", None, "api_error"),
        ("stopped", "budget exceeded: 2000000 tokens used", None, "budget_exhausted"),
        ("stopped", "budget_exceeded", None, "budget_exhausted"),
        ("stopped", "team budget exceeded: aggregate spend reached the global cap", None, "budget_exhausted"),
        ("stopped", "output truncated: provider reached its generation limit", None, "provider_truncated"),
        ("stopped", "step limit reached: 60 steps", None, "step_limit"),
        ("stopped", "context overflow: prompt exceeds the model context window even after compaction", None, "context_overflow"),
        ("stopped", "timeout", None, "wall_clock_timeout"),
        ("failed", "ProgrammaticLifecycleError: agent trajectory persistence failed", None, "harness_error"),
        ("failed", "RunError: run failed", None, "harness_error"),
        ("failed", "workflow manifest finalization failed", None, "harness_error"),
    ],
)
def test_classify_run_maps_source_derived_shapes(status, reason, error, expected):
    assert scan_batch.classify_run(status, reason, error)[0] == expected


def test_validity_is_decided_per_quantity_not_per_run():
    """Coordinator ruling 2026-09-01: one run, two different verdicts."""
    # Budget stops carry real information about delegation propensity...
    assert "budget_exhausted" in scan_batch.ALPHA_VALID_CLASSES
    assert "provider_truncated" in scan_batch.ALPHA_VALID_CLASSES
    # ...but are right-censored on token spend and resolve outcome.
    assert "budget_exhausted" not in scan_batch.OUTCOME_VALID_CLASSES
    assert "provider_truncated" not in scan_batch.OUTCOME_VALID_CLASSES
    assert scan_batch.OUTCOME_VALID_CLASSES == frozenset({"completed"})
    # A mid-flight death is invalid for everything: the model never finished
    # the decision process being measured.
    for klass in ("api_error", "harness_error"):
        assert klass not in scan_batch.ALPHA_VALID_CLASSES
        assert klass not in scan_batch.OUTCOME_VALID_CLASSES
    # 2026-09-07: ``wall_clock_timeout`` joined the alpha denominator, on the
    # ruling above ALPHA_VALID_CLASSES -- the wall clock is a budget, and a run
    # it ends had the same allowance as every other run and did not deliver.
    # It is censored for OUTCOME on the same footing as a token stop: its
    # token total is a cap rather than a spend. Note this does NOT decide the
    # resolve denominator the paper reports, which is every instance in the
    # cell; see the OUTCOME_VALID_CLASSES ruling.
    assert "wall_clock_timeout" in scan_batch.ALPHA_VALID_CLASSES
    assert "wall_clock_timeout" not in scan_batch.OUTCOME_VALID_CLASSES
    assert scan_batch.CENSORED_FOR_OUTCOME == frozenset(
        {"budget_exhausted", "provider_truncated", "wall_clock_timeout"}
    )


def test_a_budget_stop_is_kept_for_alpha_and_censored_for_cost():
    cell = scan_batch.scan_cell(MIXED)
    run = next(r for r in cell["runs"] if r["instance_id"] == "acme__acme-3")
    assert run["validity_class"] == "budget_exhausted"
    assert run["valid_for_alpha"] is True
    assert run["valid_for_outcome"] is False
    assert run["censored_for_outcome"] is True
    # its token total is a cap, so it must not appear among the spends
    assert run["tokens"] in cell["tokens_censored"]
    assert run["tokens"] not in cell["tokens_outcome"]


def test_an_api_error_is_invalid_for_both_quantities():
    cell = scan_batch.scan_cell(MIXED)
    run = next(r for r in cell["runs"] if r["instance_id"] == "acme__acme-2")
    assert run["validity_class"] == "api_error"
    assert run["valid_for_alpha"] is False
    assert run["valid_for_outcome"] is False
    assert run["censored_for_outcome"] is False


def test_an_unseen_reason_is_loudly_unclassified_not_quietly_valid():
    klass, evidence = scan_batch.classify_run("stopped", "brand new runtime string", None)
    assert klass == scan_batch.UNCLASSIFIED
    assert klass not in scan_batch.ALPHA_VALID_CLASSES
    assert klass not in scan_batch.OUTCOME_VALID_CLASSES
    assert "brand new runtime string" in evidence


# --------------------------------------------------------------------------
# 2. Denominators use valid runs only.  This is the think-decide-first bug.
# --------------------------------------------------------------------------

def test_mid_flight_deaths_are_excluded_from_the_alpha_denominator():
    cell = scan_batch.scan_cell(MIXED)
    assert cell["n_records"] == 7
    # 1 completed + 2 budget + 1 truncated; the 2 api_error and 1 harness go.
    assert cell["n_valid_alpha"] == 4
    assert cell["n_dropped_alpha"] == 3
    assert cell["alpha_den"] == 4
    assert cell["alpha_num"] == 2
    assert cell["alpha"] == pytest.approx(0.5)


def test_the_old_all_runs_denominator_is_reported_beside_it():
    cell = scan_batch.scan_cell(MIXED)
    # Both api_error runs also delegated, so the old way gives 4/7 -- a
    # different value, not merely a different denominator.
    assert (cell["alpha_all_num"], cell["alpha_all_den"]) == (4, 7)
    assert cell["alpha_all"] != cell["alpha"]


def test_the_outcome_denominator_is_smaller_and_says_what_it_held_out():
    cell = scan_batch.scan_cell(MIXED)
    assert cell["n_valid_outcome"] == 1
    assert cell["n_censored_outcome"] == 3
    assert cell["censored_outcome_by_class"] == {
        "budget_exhausted": 2, "provider_truncated": 1,
    }
    assert cell["dropped_outcome_by_class"] == {"api_error": 2, "harness_error": 1}
    # the two denominators genuinely differ, which is the whole point
    assert cell["alpha_den"] != cell["n_valid_outcome"]


def test_every_class_is_counted_so_any_denominator_can_be_recomputed():
    cell = scan_batch.scan_cell(MIXED)
    assert cell["class_counts"] == {
        "api_error": 2,
        "budget_exhausted": 2,
        "completed": 1,
        "harness_error": 1,
        "provider_truncated": 1,
    }
    assert sum(cell["class_counts"].values()) == cell["n_records"]


def test_dropped_alpha_classes_are_named_separately():
    cell = scan_batch.scan_cell(MIXED)
    assert cell["dropped_alpha_by_class"] == {"api_error": 2, "harness_error": 1}


def test_strict_mode_collapses_alpha_onto_the_outcome_denominator():
    cell = scan_batch.scan_cell(MIXED, scan_batch.OUTCOME_VALID_CLASSES)
    assert cell["alpha_den"] == 1
    assert cell["n_dropped_alpha"] == 6


def test_treat_valid_can_widen_the_alpha_denominator_further():
    cell = scan_batch.scan_cell(MIXED, scan_batch.ALPHA_VALID_CLASSES | {"harness_error"})
    assert cell["alpha_den"] == 5


# --------------------------------------------------------------------------
# 3. The positive control on the *output*: exclusions are always printed.
# --------------------------------------------------------------------------

def test_exclusion_report_names_each_discarded_run_and_its_reason():
    cells = [scan_batch.scan_cell(MIXED)]
    text = scan_batch.render_text(cells)
    assert "剔除 3 个" in text
    for instance in ("acme__acme-2", "acme__acme-5", "acme__acme-7"):
        assert instance in text
    for klass in ("api_error", "budget_exhausted", "provider_truncated", "harness_error"):
        assert klass in text
    # the discarded runs are pointed at by file line, not just counted
    assert "metrics.jsonl:2" in text


def test_the_report_prints_both_denominators_and_what_each_held_out():
    cells = [scan_batch.scan_cell(MIXED)]
    text = scan_batch.render_text(cells)
    assert "α 的分母" in text
    assert "resolve/成本 的分母" in text
    # censored runs are named as censored, not as failures
    assert "右截尾 3 个" in text
    assert "acme__acme-3" in text and "acme__acme-6" in text
    # and the two rates are printed with their own denominators
    assert "2/4 = 0.50" in text     # alpha
    assert "4/7 = 0.57" in text     # alpha, old way, beside it


def test_the_report_says_which_seat_burned_the_budget():
    cells = [scan_batch.scan_cell(MIXED)]
    text = scan_batch.render_text(cells)
    assert "预算烧光发生在哪个座位" in text
    assert "{0: 1, 1: 1}" in text


def test_zero_exclusions_still_prints_a_line():
    """'no dead runs' and 'never checked' must not look the same.

    Asserted on section A's full sentence, not the bare substring "剔除 0 个":
    section B prints that substring for a clean cell too, so the short form
    passed even with section A's zero line deleted.
    """
    cells = [scan_batch.scan_cell(CLEAN)]
    text = scan_batch.render_text(cells)
    section_a = text.split("--- 剔除报告 B")[0]
    assert "剔除 0 个 —— 2 个 run 全部进 α 分母" in section_a


def test_unclassified_run_reaches_the_printed_report():
    cells = [scan_batch.scan_cell(UNKNOWN)]
    text = scan_batch.render_text(cells)
    assert scan_batch.UNCLASSIFIED in text
    assert "剔除 1 个" in text


# --------------------------------------------------------------------------
# 4. Mechanism quantities.
# --------------------------------------------------------------------------

def test_per_seat_quantities_are_split_by_aid():
    cell = scan_batch.scan_cell(MIXED)
    run = next(r for r in cell["runs"] if r["instance_id"] == "acme__acme-1")
    assert run["message_agent_calls"] == 1
    assert run["message_agent_by_aid"] == {0: 1}
    assert run["team_status_calls"] == 1
    assert run["write_tool_calls_by_aid"] == {1: 1}      # the coder wrote, not the analyst
    assert run["bash_write_calls_by_aid"] == {2: 1}
    assert run["tokens_by_aid"] == {0: 500_000, 1: 300_000, 2: 100_000}
    assert run["roles_by_aid"] == {0: "analyst", 1: "coder", 2: "tester"}
    assert run["patch_chars"] == 1500
    assert run["delegated"] is True


ANALYST_REASONING = (
    "I should hand this to the coder; delegate the edit.",
    "Now run the test suite to verify the patch and fix it.",
)
CODER_REASONING = "As the coder I apply the patch the coder was asked for."


def test_the_seat_that_ran_out_of_budget_is_identified():
    """Which seat ran out is a different fact from the run running out.

    acme-3 and acme-6 carry the SAME run-level label and opposite meanings for
    alpha: in acme-3 the analyst ran out before any handover; in acme-6 the
    handover happened and the coder ran out afterwards.
    """
    cell = scan_batch.scan_cell(MIXED)
    analyst_burn = next(r for r in cell["runs"] if r["instance_id"] == "acme__acme-3")
    coder_burn = next(r for r in cell["runs"] if r["instance_id"] == "acme__acme-6")
    assert analyst_burn["validity_class"] == coder_burn["validity_class"] == "budget_exhausted"
    assert analyst_burn["budget_exhausted_aids"] == [0]
    assert coder_burn["budget_exhausted_aids"] == [1]
    assert analyst_burn["delegated"] is False
    assert coder_burn["delegated"] is True
    assert cell["budget_exhausted_seat_counts"] == {0: 1, 1: 1}


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("budget exceeded: 1000000 tokens used", "budget_exhausted"),
        ("output truncated: provider reached its generation limit", "provider_truncated"),
        ("step limit reached: 60 steps", "step_limit"),
        (None, None),
        ("", None),
        ("something no runtime ever wrote", scan_batch.UNCLASSIFIED),
    ],
)
def test_classify_seat_reason_reads_the_seat_field(reason, expected):
    assert scan_batch.classify_seat_reason(reason) == expected


# --------------------------------------------------------------------------
# 4b. The seat classifier against the strings real seats actually wrote.
#
# Every case below is one of the 16 distinct ``runs[].seats[].terminal`` strings
# found across the 1,673 seat records of the 26 batch reports in
# /root/oc-batches (2026-09-06), copied verbatim, with the count that string had
# in that census beside it.  Before ``_SEAT_RULES`` existed the classifier read
# only the ``status == "stopped"`` half of ``_RULES``, so 11 of these 16
# families -- 960 of the 1,673 records, "submitted" the largest single family
# among them -- came back UNCLASSIFIED.
# --------------------------------------------------------------------------

REAL_SEAT_REASONS = [
    # (n in the census, verbatim reason, expected class)
    (624, "submitted", "submitted"),
    (542, "", None),
    (184, "interrupted by user", "interrupted"),
    (
        156,
        "budget exhausted before model call: conservative input reservation "
        "requires 57215 of 43898 remaining tokens, leaving no output headroom",
        "budget_exhausted",
    ),
    (
        41,
        "InternalServerError: Error code: 503 - {'error': {'message': "
        "'Service temporarily unavailable', 'type': 'api_error'}}",
        "api_error",
    ),
    (36, "completed", "completed"),
    (30, "APIError: Concurrency limit exceeded for user, please retry later", "api_error"),
    (16, "error", "seat_error"),
    (13, "cancelled", "cancelled"),
    (11, "loop block limit reached: 3 repeated tool calls", "loop_block"),
    (
        6,
        "RateLimitError: Error code: 429 - {'error': {'message': "
        "'Upstream rate limit exceeded, please retry later', "
        "'type': 'rate_limit_error'}}",
        "api_error",
    ),
    (4, "output truncated: provider reached its generation limit", "provider_truncated"),
    (4, "APIError: Upstream response stream was interrupted", "api_error"),
    (3, "Error: scheduler cleanup cancelled delegated work", "cleanup_cancelled"),
    (2, "APIError: Upstream HTTP/2 stream failed", "api_error"),
    (1, "APIError: Upstream request failed", "api_error"),
]


@pytest.mark.parametrize(
    "n,reason,expected",
    REAL_SEAT_REASONS,
    ids=[f"{n}x-{expected}" for n, _, expected in REAL_SEAT_REASONS],
)
def test_every_seat_reason_family_on_record_is_classified(n, reason, expected):
    assert scan_batch.classify_seat_reason(reason) == expected


def test_no_seat_reason_family_on_record_is_unclassified():
    """The census as a whole, not case by case: nothing observed falls through.

    Stated as its own assertion because the parametrised cases above would still
    pass one at a time if a rule quietly widened and swallowed a neighbour.
    """
    unmatched = [
        r for _, r, _ in REAL_SEAT_REASONS
        if scan_batch.classify_seat_reason(r) == scan_batch.UNCLASSIFIED
    ]
    assert unmatched == []
    covered = sum(n for n, _, _ in REAL_SEAT_REASONS)
    assert covered == 1673, "the census total moved; re-derive the table"


@pytest.mark.parametrize(
    "reason,expected",
    [
        # Source-derived, never seen in the census; kept so a future runtime
        # string does not go UNCLASSIFIED the day it first appears.
        ("context overflow: prompt exceeds the model context window even after compaction",
         "context_overflow"),
        ("wind-down complete: forced commit within reserve", "wind_down"),
        ("timeout", "wall_clock_timeout"),
        ("budget reserve exhausted: protected commit turn already used", "budget_exhausted"),
        ("team budget exceeded: aggregate spend reached the global cap", "budget_exhausted"),
        # Every api_error string in the census also carries "upstream",
        # "concurrency limit exceeded", "InternalServerError" or
        # "RateLimitError", so the census alone cannot tell whether the SDK
        # class-name branch of the rule works. These three carry none of that
        # wording, so only that branch can match them -- without these, deleting
        # it from the pattern leaves the suite green (checked by mutation).
        ("APIError: model produced no output", "api_error"),
        ("APIStatusError", "api_error"),
        ("APIConnectionError", "api_error"),
        # An unseen string still has to be loud, not quietly folded into a class.
        ("brand new runtime string nobody has written yet", scan_batch.UNCLASSIFIED),
        ("   ", None),
    ],
)
def test_classify_seat_reason_on_strings_not_in_the_census(reason, expected):
    assert scan_batch.classify_seat_reason(reason) == expected


def test_the_narrow_seat_rules_win_over_the_wide_ones():
    """First-match-wins order, asserted on the three pairs that actually collide.

    Each left-hand string is also matched by a wider rule further down the
    table; if the order were sorted differently these would silently change
    class rather than fail.
    """
    # contains "cancelled", which the bare ``cancelled`` rule also matches
    assert scan_batch.classify_seat_reason(
        "Error: scheduler cleanup cancelled delegated work") == "cleanup_cancelled"
    assert scan_batch.classify_seat_reason("cancelled") == "cancelled"
    # contains "provider", which the wide api_error rule also matches
    assert scan_batch.classify_seat_reason(
        "output truncated: provider reached its generation limit") == "provider_truncated"
    # contains "interrupted", but not "interrupted by user"
    assert scan_batch.classify_seat_reason(
        "APIError: Upstream response stream was interrupted") == "api_error"
    assert scan_batch.classify_seat_reason("interrupted by user") == "interrupted"


def test_the_seat_table_is_separate_from_the_run_table():
    """The seat rules must not be a filtered view of ``_RULES``.

    Positive control on the claim the old docstring made: ``_RULES`` restricted
    to ``status == "stopped"`` genuinely cannot classify these seat strings, so
    a scanner that reuses it is not merely stylistically different.
    """
    stopped_only = [
        (cls, pat) for cls, status, pat in scan_batch._COMPILED
        if status == "stopped" and pat is not None
    ]

    def old_way(text):
        for cls, pattern in stopped_only:
            if pattern.search(text):
                return cls
        return scan_batch.UNCLASSIFIED

    for text in ("submitted", "completed", "cancelled", "error",
                 "APIError: Concurrency limit exceeded for user, please retry later",
                 "Error: scheduler cleanup cancelled delegated work"):
        assert old_way(text) == scan_batch.UNCLASSIFIED
        assert scan_batch.classify_seat_reason(text) != scan_batch.UNCLASSIFIED


def test_preemptive_seat_classes_mark_where_a_zero_delegation_count_is_censored():
    """A seat cut off from outside cannot testify that the run chose not to delegate."""
    for klass in ("budget_exhausted", "provider_truncated", "cancelled",
                  "cleanup_cancelled", "api_error", "seat_error",
                  "interrupted", "wall_clock_timeout"):
        assert klass in scan_batch.SEAT_PREEMPTIVE_CLASSES
    # A seat that reached its own terminal action had the whole run to delegate.
    assert scan_batch.SEAT_NATURAL_END_CLASSES == frozenset({"submitted", "completed"})
    assert not (scan_batch.SEAT_NATURAL_END_CLASSES & scan_batch.SEAT_PREEMPTIVE_CLASSES)
    # loop_block is deliberately in neither: the record does not say whether the
    # loop happened before or after the intent to delegate was formed.
    assert scan_batch.SEAT_UNDETERMINED_CLASSES == frozenset({"loop_block"})
    assert "loop_block" not in scan_batch.SEAT_PREEMPTIVE_CLASSES
    assert "loop_block" not in scan_batch.SEAT_NATURAL_END_CLASSES


def test_every_seat_class_the_table_can_emit_is_placed_in_exactly_one_set():
    """No class may sit outside the three sets: that is how one gets forgotten."""
    emitted = {cls for cls, _ in scan_batch._SEAT_RULES}
    placed = (scan_batch.SEAT_PREEMPTIVE_CLASSES
              | scan_batch.SEAT_NATURAL_END_CLASSES
              | scan_batch.SEAT_UNDETERMINED_CLASSES)
    assert emitted == placed, f"unplaced: {emitted - placed}; unknown: {placed - emitted}"
    counts = [
        sum(cls in s for s in (scan_batch.SEAT_PREEMPTIVE_CLASSES,
                               scan_batch.SEAT_NATURAL_END_CLASSES,
                               scan_batch.SEAT_UNDETERMINED_CLASSES))
        for cls in emitted
    ]
    assert counts == [1] * len(emitted)


def test_reasoning_is_counted_only_for_aid_zero_and_carries_a_positive_control():
    cell = scan_batch.scan_cell(MIXED)
    run = next(r for r in cell["runs"] if r["instance_id"] == "acme__acme-1")
    # Exact, not ">0": the coder seat's reasoning must not be in this total.
    assert run["reasoning_chars_aid0"] == sum(len(t) for t in ANALYST_REASONING)
    assert CODER_REASONING.lower().count("coder") == 2  # control on the fixture
    # ...so a scanner counting every seat would report 3 here, not 1.
    assert run["reasoning_teammate_hits_by_term"]["coder"] == 1
    assert run["reasoning_teammate_hits_by_term"]["delegat"] == 1
    # Control terms: a zero teammate count only means something when this is >0.
    assert run["reasoning_control_hits"] > 0


def test_a_metrics_row_with_no_trajectory_is_not_counted_as_a_silent_zero(tmp_path):
    cell = tmp_path / "cell-ghost"
    cell.mkdir()
    (cell / "metrics.jsonl").write_text(
        json.dumps({
            "instance_id": "ghost__ghost-1",
            "submitted_patch_chars": 0,
            "run_summary": {"steps": 3, "tokens": 10, "status": "completed",
                            "reason": None, "duration_s": 1.0, "error": None},
        }) + "\n",
        encoding="utf-8",
    )
    result = scan_batch.scan_cell(str(cell))
    assert result["n_valid_alpha"] == 0
    assert result["n_valid_outcome"] == 0
    assert result["dropped_alpha_by_class"] == {scan_batch.NO_TRAJECTORY: 1}


# --------------------------------------------------------------------------
# 5. Parameterisation and machine-readable output.
# --------------------------------------------------------------------------

def _code_without_comments_or_docs(path: str) -> str:
    """Source with ``#`` comments and the DEFERRED_VERIFICATION note removed,
    but with every other string literal kept.

    Comments and that one note must go: both legitimately *name* the gpu3 root
    while telling a reader to go verify against it.  Every other string must
    stay -- that is exactly where think_scan.py hides its root
    (``os.path.expanduser("~/oc-team-smoke")``), so a check that stripped all
    strings would also pass on the old scanner and would prove nothing.
    """
    import ast
    import io
    import tokenize

    raw = open(path, "rb").read()
    kept = []
    for tok in tokenize.tokenize(io.BytesIO(raw).readline):
        if tok.type == tokenize.COMMENT:
            continue
        kept.append(tok.string)
    text = " ".join(kept)

    source = raw.decode("utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "DEFERRED_VERIFICATION"
            for t in node.targets
        ):
            # The token stream keeps a string's *source* form (escapes and all),
            # so remove the source segment, not the parsed value.
            segment = ast.get_source_segment(source, node.value)
            if segment:
                text = text.replace(segment, "")
    return text


def test_no_hardcoded_data_root_in_the_new_scanner():
    new_code = _code_without_comments_or_docs(os.path.join(SCRIPTS, "scan_batch.py"))
    old_code = _code_without_comments_or_docs(os.path.join(SCRIPTS, "think_scan.py"))
    # Positive control first: the check must find the root when it is there.
    assert "oc-team-smoke" in old_code, "the check cannot detect a hard-coded root"
    assert "oc-team-smoke" not in new_code


def test_the_scanner_reads_whatever_directory_it_is_handed(tmp_path):
    """Parameterisation, asserted behaviourally rather than by grep."""
    import shutil

    moved = tmp_path / "somewhere" / "else" / "a-cell-with-no-think-prefix"
    shutil.copytree(MIXED, moved)
    result = scan_batch.scan_cell(str(moved))
    assert result["n_records"] == 7
    assert result["alpha_den"] == 4


def test_cli_scans_several_cells_and_writes_stable_json(tmp_path):
    out = tmp_path / "scan.json"
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "scan_batch.py"),
         MIXED, CLEAN, "--json", str(out)],
        capture_output=True, text=True, check=True,
    )
    assert "剔除 3 个" in proc.stdout
    assert "剔除 0 个 —— 2 个 run 全部进 α 分母" in proc.stdout
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema_version"] == scan_batch.SCHEMA_VERSION
    assert doc["criterion_verified_against_real_records"] is False
    assert doc["alpha_valid_classes"] != doc["outcome_valid_classes"]
    assert doc["censored_for_outcome_classes"] == [
        "budget_exhausted", "provider_truncated", "wall_clock_timeout"
    ]
    by_cell = {c["cell"]: c for c in doc["cells"]}
    assert by_cell["cell-mixed"]["alpha_den"] == 4
    assert by_cell["cell-mixed"]["alpha_all_den"] == 7
    assert by_cell["cell-mixed"]["n_valid_outcome"] == 1
    assert by_cell["cell-clean"]["n_dropped_alpha"] == 0
    assert len(doc["runs"]) == 9
    for run in doc["runs"]:
        assert set(scan_batch.CSV_FIELDS) <= set(run)


def test_strict_flag_is_reachable_from_the_command_line(tmp_path):
    out = tmp_path / "strict.json"
    subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "scan_batch.py"), MIXED,
         "--strict", "--json", str(out)],
        capture_output=True, text=True, check=True,
    )
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["alpha_valid_classes"] == doc["outcome_valid_classes"] == ["completed"]
    assert doc["cells"][0]["alpha_den"] == 1


# --------------------------------------------------------------------------
# 6. The two arms whose seats are not where the team arm keeps them.
#
# ``_find_runtime_dir`` looked for ``team.json``, which only the team arm
# writes, and ``measure_trajectory`` globbed ``agent_*.json``, which only the
# team arm names its seats. So on 2026-09-04 every single-arm run read as
# ``no_trajectory`` and every DW run as ``no_trajectory``/``unclassified``,
# with every seat column zero -- the same output a run whose seats did nothing
# produces. The cell report's JSON, reading the same directories, had the real
# numbers, so the text table and the JSON disagreed on the same runs.
#
#   single: ``<cell>/agent-<hex>/agent.json``, at the batch root, named by a
#           random id that carries no instance; the tie is the run's own
#           ``trajectory_path``.
#   DW:     ``logs-self-collaboration/<instance>/trajectories/*/runtime-*/
#           <nnn>_<label>.json``, with no ``team.json`` anywhere and one
#           generic ``workflow_agent`` role stamped on every seat, so a seat's
#           identity is in its file name.
# --------------------------------------------------------------------------

def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload))


def _seat(aid, role, tokens, assistant=1, tool=None, terminal=None):
    messages = []
    for i in range(assistant):
        message = {"role": "assistant", "content": f"turn {i}"}
        if tool:
            message["tool_calls"] = [{"function": {"name": tool, "arguments": "{}"}}]
        messages.append(message)
    return {
        "aid": aid,
        "role": role,
        "model": "m",
        "session_state": {
            "used_tokens": tokens,
            "step_count": assistant,
            "terminal_reason": terminal,
        },
        "messages": messages,
    }


def _single_cell(tmp_path):
    cell = os.path.join(str(tmp_path), "single-cell")
    seat_dir = os.path.join(cell, "agent-eaa70723b45941d3a4297ec831f1c251")
    _write(os.path.join(seat_dir, "agent.json"), _seat(-1, "swe_agent", 1_939_494, assistant=3, tool="apply_patch"))
    with open(os.path.join(cell, "metrics.jsonl"), "w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "instance_id": "solo__solo-1",
            "submitted_patch_chars": 2326,
            # Written on the machine that ran the batch: the directory is here,
            # the path in front of it is not.
            "trajectory_path": "/home/u/oc-team-smoke/single-cell/agent-eaa70723b45941d3a4297ec831f1c251",
            "run_summary": {"status": "completed", "reason": None, "tokens": 1_939_494,
                            "steps": 3, "duration_s": 10.0, "error": None},
        }) + "\n")
    return cell


def _dw_cell(tmp_path):
    cell = os.path.join(str(tmp_path), "dw-cell")
    runtime = os.path.join(cell, "logs-self-collaboration", "flow__flow-1",
                           "trajectories", "solver-aa", "runtime-bb")
    # The analyst is seated twice: once to write the brief, once to adjudicate.
    _write(os.path.join(runtime, "000_analyst.json"), _seat(0, "workflow_agent", 603_787, assistant=4))
    _write(os.path.join(runtime, "001_coder-r1.json"), _seat(1, "workflow_agent", 55_307, assistant=2, tool="apply_patch"))
    _write(os.path.join(runtime, "002_tester-r1.json"), _seat(2, "workflow_agent", 69_179, assistant=2))
    _write(os.path.join(runtime, "003_analyst-adjudicate-r1.json"), _seat(3, "workflow_agent", 43_485, assistant=1))
    _write(os.path.join(runtime, "workflow.json"), {"workflow": "self_collaboration", "sessions": 4})
    with open(os.path.join(cell, "metrics.jsonl"), "w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "instance_id": "flow__flow-1",
            "submitted_patch_chars": 777,
            "run_summary": {"status": "completed", "reason": None, "tokens": 771_758,
                            "steps": 9, "duration_s": 20.0, "error": None},
        }) + "\n")
    return cell


def test_a_single_arm_run_s_seat_is_found_through_its_record(tmp_path):
    cell = scan_batch.scan_cell(_single_cell(tmp_path))
    run = cell["runs"][0]
    assert run["trajectory_found"] is True
    assert run["validity_class"] == "completed"
    assert run["n_agent_files"] == 1
    assert run["tokens_by_role"] == {"swe_agent": 1_939_494}
    assert run["assistant_msgs_by_role"] == {"swe_agent": 3}
    assert run["write_tool_calls_by_aid"] == {-1: 3}


def test_a_workflow_run_s_seats_are_found_and_named_by_their_files(tmp_path):
    cell = scan_batch.scan_cell(_dw_cell(tmp_path))
    run = cell["runs"][0]
    assert run["trajectory_found"] is True
    assert run["validity_class"] == "completed"
    assert run["n_agent_files"] == 4
    # The runtime stamps one generic role on every seat, so the identity is in
    # the file name; the analyst's two seats are summed, not overwritten.
    assert run["roles_by_aid"] == {0: "analyst", 1: "coder", 2: "tester", 3: "analyst"}
    assert run["tokens_by_role"] == {"analyst": 647_272, "coder": 55_307, "tester": 69_179}
    assert run["seats_by_role"] == {"analyst": 2, "coder": 1, "tester": 1}


def test_a_team_run_is_still_found_and_read_by_role(tmp_path):
    """The control: the arm that did work keeps working, and gains the totals."""
    run = next(r for r in scan_batch.scan_cell(MIXED)["runs"] if r["instance_id"] == "acme__acme-1")
    assert run["trajectory_found"] is True
    assert run["tokens_by_role"] == {"analyst": 500_000, "coder": 300_000, "tester": 100_000}
    assert run["seats_by_role"] == {"analyst": 1, "coder": 1, "tester": 1}


def test_the_per_run_line_prints_the_role_totals_not_three_fixed_aids(tmp_path):
    text = scan_batch.render_text([scan_batch.scan_cell(_dw_cell(tmp_path))])
    detail = next(line for line in text.splitlines() if "flow__flow-1" in line and line.startswith("["))
    assert "no_trajectory" not in detail
    assert "analyst=647,272" in detail and "coder=55,307" in detail and "tester=69,179" in detail
