#!/usr/bin/env python3
"""Mutation check for scan_batch.py.

Break the validity criterion (and the reporting that surrounds it) one way at a
time, and require the test suite to go red each time.  A mutation that leaves
the suite green means the corresponding test is not actually pinning anything.

Run:  python3 mutate.py
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(os.path.dirname(HERE), "scan_batch.py")
TESTS = os.path.join(HERE, "test_scan_batch.py")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
PYTEST = "/root/git/OpenCollab/.venv/bin/pytest"
# A rule that does not live in the scanner is broken in the file it does live
# in, and the suite that has to notice is that file's own. The evaluator's
# suite needs `opencollab_eval` importable, which the scanner's environment has
# no reason to carry, so it runs under the evaluator's.
EVAL_PYTEST = "/root/git/OpenCollab-Eval/.venv/bin/pytest"
BATCH_CLI = os.path.join(REPO, "src", "opencollab_eval", "commands", "batch.py")
BATCH_REPLACEMENT_TESTS = os.path.join(REPO, "tests", "test_batch_replacement.py")

# (name, what it breaks, old fragment, new fragment) -- and, for a rule outside
# scan_batch.py, the file to break and the suite that has to go red.
MUTATIONS = [
    (
        "M1 everything-is-valid",
        "classify_run always returns the valid class",
        '    s = "" if status is None else str(status).strip().lower()',
        '    return "completed", "MUTANT"\n    s = "" if status is None else str(status).strip().lower()',
    ),
    (
        "M2 lose the budget rule",
        "budget stops fall through to unclassified",
        '("budget_exhausted", "stopped", r"budget[_ ]exceeded|budget exhausted|budget reserve exhausted"),',
        "",
    ),
    (
        "M3 lose the provider-truncation rule",
        "provider truncation falls through to unclassified",
        '("provider_truncated", "stopped", r"output truncated: provider reached its generation limit"),',
        "",
    ),
    (
        "M4 api_error stops matching",
        "an APIError abort is no longer recognised as a provider fault",
        "r\"APIError|APIStatusError|APIConnectionError|APITimeoutError\"",
        "r\"__NEVER_MATCHES_ANYTHING__\"",
    ),
    (
        "M5 alpha over all runs",
        "the dead runs go back into the denominator (the original bug)",
        '        "alpha_num": len(delegating_alpha),\n        "alpha_den": len(a_valid),',
        '        "alpha_num": len(delegating_all),\n        "alpha_den": len(runs),',
    ),
    (
        "M6 silence the zero-exclusion line",
        "'no dead runs' becomes indistinguishable from 'never checked'",
        '            add(f"{cell[\'cell\']:<26s} 剔除 0 个 —— {cell[\'n_records\']} 个 run 全部进 α 分母")',
        "            pass",
    ),
    (
        "M7 unknown reasons count as valid",
        "a runtime string we have never seen silently enters the denominator",
        'ALPHA_VALID_CLASSES = frozenset(\n    {"completed", "budget_exhausted", "provider_truncated", "wall_clock_timeout"}\n)',
        'ALPHA_VALID_CLASSES = frozenset(\n    {"completed", "budget_exhausted", "provider_truncated", "wall_clock_timeout",\n     "unclassified"}\n)',
    ),
    (
        "M8 reasoning counted for every seat",
        "aid=0 reasoning volume stops being an analyst-only quantity",
        "            if reasoning and aid == 0:",
        "            if reasoning:",
    ),
    (
        "M9 a missing trajectory counts as a run",
        "a metrics row with no trajectory becomes a silent zero",
        "        if runtime_dir is None and klass in (alpha_valid | outcome_valid):",
        "        if False:",
    ),
    (
        "M10 stop naming the discarded runs",
        "the report says how many were dropped but not which or why",
        "            for bad in cell[\"dropped_alpha_runs\"]:",
        "            for bad in []:",
    ),
    (
        "M11 write tools no longer split by seat",
        "delivery-line attribution collapses onto one seat",
        "                elif name in WRITE_TOOLS:\n                    write_by_aid[aid] += 1",
        "                elif name in WRITE_TOOLS:\n                    write_by_aid[0] += 1",
    ),
    (
        "M13 budget stops dropped from alpha",
        "the most informative class is thrown out of the delegation rate",
        'ALPHA_VALID_CLASSES = frozenset(\n    {"completed", "budget_exhausted", "provider_truncated", "wall_clock_timeout"}\n)',
        'ALPHA_VALID_CLASSES = frozenset({"completed"})',
    ),
    (
        "M21 wall-clock stops dropped from alpha",
        "a run the clock ended is treated as an instrument failure again",
        'ALPHA_VALID_CLASSES = frozenset(\n    {"completed", "budget_exhausted", "provider_truncated", "wall_clock_timeout"}\n)',
        'ALPHA_VALID_CLASSES = frozenset(\n    {"completed", "budget_exhausted", "provider_truncated"}\n)',
    ),
    (
        "M14 censored runs merged into the cost sample",
        "capped token totals are averaged in as if they were spends",
        'OUTCOME_VALID_CLASSES = frozenset({"completed"})',
        'OUTCOME_VALID_CLASSES = frozenset({"completed", "budget_exhausted", "provider_truncated"})',
    ),
    (
        "M15 one denominator for both quantities",
        "the per-quantity ruling collapses back to a single valid set",
        '            "valid_for_outcome": klass in outcome_valid,',
        '            "valid_for_outcome": klass in alpha_valid,',
    ),
    (
        "M16 seat-level burnout not recorded",
        "analyst-burned and coder-burned runs become indistinguishable",
        "                if seat_class == \"budget_exhausted\":\n                    out[\"budget_exhausted_aids\"].append(aid)",
        "                if False:\n                    out[\"budget_exhausted_aids\"].append(aid)",
    ),
    (
        "M17 seat reason classifier always says budget",
        "every stopped seat is read as a budget burnout",
        "    for cls, pattern in _SEAT_COMPILED:\n        if pattern.search(text):",
        "    return \"budget_exhausted\"\n    for cls, pattern in _SEAT_COMPILED:\n        if pattern.search(text):",
    ),
    (
        "M19 seats read through the stopped-half of _RULES again",
        "the seat classifier goes back to the run table, so 'submitted', "
        "'completed', 'cancelled', 'error' and every provider fault on record "
        "fall through to unclassified",
        "    for cls, pattern in _SEAT_COMPILED:\n        if pattern.search(text):",
        "    for cls, _st, pattern in _COMPILED:\n        if _st != \"stopped\" or pattern is None:\n            continue\n        if pattern.search(text):",
    ),
    (
        "M20 loop_block declared preemptive",
        "a class whose position relative to the delegation decision is unknown "
        "is asserted to be censoring",
        '    "step_limit", "context_overflow", "wind_down",\n})',
        '    "step_limit", "context_overflow", "wind_down", "loop_block",\n})',
    ),
    (
        "M18 censored runs reported as plain drops",
        "right-censoring stops being visible as its own category",
        '        "censored_outcome_by_class": _by_class(o_censored),',
        '        "censored_outcome_by_class": {},',
    ),
    (
        "M22 a withdrawal that changes no denominator",
        "the report says a replacement was withdrawn while the cell still "
        "reports the stand-in and still drops the instance it was drawn for -- "
        "the ledger and the excluded row look right and every rate is the "
        "pre-withdrawal one",
        "        stand_ins = [r for r in stand_ins if r.replacement_for not in withdrawals]\n"
        "        replaced = {gone: arrives for gone, arrives in replaced.items() if gone not in withdrawals}",
        "        pass",
        BATCH_CLI,
        BATCH_REPLACEMENT_TESTS,
    ),
    (
        "M12 re-introduce a hard-coded root",
        "the scanner stops being parameterised",
        "SCHEMA_VERSION = 1",
        'SCHEMA_VERSION = 1\nHOME = os.path.expanduser("~/oc-team-smoke")',
    ),
]


def normalised() -> list[tuple[str, str, str, str, str, str]]:
    """Every mutation as (name, effect, old, new, file to break, suite to watch)."""
    return [m if len(m) == 6 else (*m, TARGET, TESTS) for m in MUTATIONS]


def run_tests(tests: str = TESTS) -> tuple[bool, str]:
    env = dict(os.environ)
    if tests == TESTS:
        pytest = PYTEST
    else:
        pytest = EVAL_PYTEST
        # The checkout this file is in, not whichever one the venv was
        # installed from: a worktree has to mutate and test its own source.
        env["PYTHONPATH"] = os.pathsep.join(
            [os.path.join(REPO, "src"), os.path.join(REPO, "tests"), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
    proc = subprocess.run(
        [pytest, tests, "-q", "--no-header", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=os.path.dirname(tests), env=env,
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def _write(path: str, text: str) -> None:
    open(path, "w", encoding="utf-8").write(text)


def main() -> int:
    mutations = normalised()
    originals = {path: open(path, encoding="utf-8").read() for path in {m[4] for m in mutations}}

    for tests in sorted({m[5] for m in mutations}):
        ok, out = run_tests(tests)
        if not ok:
            print(f"BASELINE IS RED ({os.path.basename(tests)}) -- fix the suite before mutating\n", out[-3000:])
            return 2
        print(f"baseline {os.path.basename(tests)}: GREEN ({out.strip().splitlines()[-1]})")
    print()

    survivors = []
    try:
        for name, effect, old, new, target, tests in mutations:
            original = originals[target]
            if old not in original:
                print(f"{name:<44s} SKIPPED (anchor not found -- mutation is stale)")
                survivors.append(name + " [stale anchor]")
                continue
            _write(target, original.replace(old, new, 1))
            try:
                ok, out = run_tests(tests)
            finally:
                _write(target, original)
            tail = out.strip().splitlines()[-1] if out.strip() else "?"
            if ok:
                print(f"{name:<44s} SURVIVED  <-- test gap: {effect}")
                survivors.append(name)
            else:
                failed = [
                    line.split("::")[-1].split(" ")[0]
                    for line in out.splitlines()
                    if line.startswith("FAILED")
                ]
                print(f"{name:<44s} KILLED    ({tail}) e.g. {failed[:3]}")
    finally:
        for path, text in originals.items():
            _write(path, text)

    print()
    for tests in sorted({m[5] for m in mutations}):
        ok, out = run_tests(tests)
        print(f"restored {os.path.basename(tests)}: {'GREEN' if ok else 'RED'} ({out.strip().splitlines()[-1]})")
    print(f"\n{len(MUTATIONS) - len(survivors)}/{len(MUTATIONS)} mutations killed")
    if survivors:
        print("survivors:", ", ".join(survivors))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
