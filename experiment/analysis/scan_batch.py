#!/usr/bin/env python3
"""Parameterised batch scanner that reports discarded runs out loud.

Replaces the hard-coded ``think_scan.py`` (kept untouched next to this file as
a control).  Two things it does that ``think_scan.py`` did not:

1.  It classifies every run as *valid* (the model itself ran to a stop) or
    *invalid*, and **every rate in the output uses only valid runs as its
    denominator**.  ``think_scan.py`` read ``think-decide-first`` as 0/3 while
    two of those three runs were ``APIError`` aborts; the honest number is 0/1.

2.  It always prints how many runs it discarded and why -- including the line
    "discarded 0" when it discarded none.  Without that line "this batch had no
    dead runs" and "the scanner never looked" are the same output, which is the
    exact failure this script exists to prevent.

Usage
-----
    python3 scan_batch.py <batch-dir> [<batch-dir> ...] \
        [--json out.json] [--csv out.csv] [--treat-valid CLASS[,CLASS...]]

Each ``<batch-dir>`` is one cell: a directory holding ``metrics.jsonl`` and,
beside it, the seat snapshots -- in whichever of the three places the arm that
ran keeps them:

    team    logs-team/<instance>/trajectories/*/runtime-*/agent_<aid>_<role>.json
    DW      logs-self-collaboration/<instance>/trajectories/*/runtime-*/<nnn>_<label>.json
    single  <cell>/agent-<hex>/agent.json          (at the batch root; the run's
                                                    own ``trajectory_path`` is
                                                    the only thing naming it)

Reading only the first of those was a silent defect, not an error: on
2026-09-04 every single-arm and every DW run came out ``no_trajectory`` with
all seat columns zero, which is exactly how a run whose seats did nothing
reads, while the cell report's JSON over the same directories had the numbers.
Per-seat quantities are also summed **by role**, because a scripted workflow
seats its analyst twice and three fixed aids print the wrong column.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import sys
from typing import Any

from scan_batch_classification import (
    _COMPILED as _COMPILED,
)
from scan_batch_classification import (
    _RULES as _RULES,
)
from scan_batch_classification import (
    _SEAT_COMPILED as _SEAT_COMPILED,
)
from scan_batch_classification import (
    _SEAT_RULES as _SEAT_RULES,
)
from scan_batch_classification import (
    ALPHA_VALID_CLASSES as ALPHA_VALID_CLASSES,
)
from scan_batch_classification import (
    CENSORED_FOR_OUTCOME as CENSORED_FOR_OUTCOME,
)
from scan_batch_classification import (
    KNOWN_STATUSES as KNOWN_STATUSES,
)
from scan_batch_classification import (
    NO_TRAJECTORY as NO_TRAJECTORY,
)
from scan_batch_classification import (
    OUTCOME_VALID_CLASSES as OUTCOME_VALID_CLASSES,
)
from scan_batch_classification import (
    SCHEMA_VERSION as SCHEMA_VERSION,
)
from scan_batch_classification import (
    SEAT_NATURAL_END_CLASSES as SEAT_NATURAL_END_CLASSES,
)
from scan_batch_classification import (
    SEAT_PREEMPTIVE_CLASSES as SEAT_PREEMPTIVE_CLASSES,
)
from scan_batch_classification import (
    SEAT_UNDETERMINED_CLASSES as SEAT_UNDETERMINED_CLASSES,
)
from scan_batch_classification import (
    UNCLASSIFIED as UNCLASSIFIED,
)
from scan_batch_classification import (
    VALID_STATUSES as VALID_STATUSES,
)
from scan_batch_classification import (
    classify_run as classify_run,
)
from scan_batch_classification import (
    classify_seat_reason as classify_seat_reason,
)
from scan_batch_measurements import (
    BASH_WRITE_RE as BASH_WRITE_RE,
)
from scan_batch_measurements import (
    GENERIC_WORKFLOW_ROLE as GENERIC_WORKFLOW_ROLE,
)
from scan_batch_measurements import (
    REASONING_CONTROL_TERMS as REASONING_CONTROL_TERMS,
)
from scan_batch_measurements import (
    REASONING_KEYS as REASONING_KEYS,
)
from scan_batch_measurements import (
    SEAT_FILE_GLOBS as SEAT_FILE_GLOBS,
)
from scan_batch_measurements import (
    SINGLE_SEAT_FILE as SINGLE_SEAT_FILE,
)
from scan_batch_measurements import (
    TEAMMATE_TERMS as TEAMMATE_TERMS,
)
from scan_batch_measurements import (
    WRITE_TOOLS as WRITE_TOOLS,
)
from scan_batch_measurements import (
    _aid_from_filename as _aid_from_filename,
)
from scan_batch_measurements import (
    _find_runtime_dir as _find_runtime_dir,
)
from scan_batch_measurements import (
    _read_metrics as _read_metrics,
)
from scan_batch_measurements import (
    _reasoning_text as _reasoning_text,
)
from scan_batch_measurements import (
    _role_from_filename as _role_from_filename,
)
from scan_batch_measurements import (
    empty_measurements as empty_measurements,
)
from scan_batch_measurements import (
    measure_trajectory as measure_trajectory,
)
from scan_batch_measurements import (
    scan_cell as scan_cell,
)

_LABEL_TOTAL = "\u5408\u8ba1"
_LABEL_CELL = "\u683c"
_LABEL_ALPHA_DEN = "α\u5206\u6bcd/\u5168\u90e8"
_LABEL_ALPHA_ALL = "α(\u65e7, \u5168\u90e8)"
_LABEL_UNCENSORED_DEN = "\u672a\u622a\u5c3e/\u5168\u90e8"
_LABEL_UNCENSORED_TOKENS = "\u672a\u622a\u5c3e token"
_LABEL_CENSORED_TOKENS = "\u622a\u5c3e token(=\u4e0a\u9650)"


def _fmt_rate(num: int, den: int, value: float | None) -> str:
    if value is None:
        return f"{num}/{den} (n/a)"
    return f"{num}/{den} = {value:.2f}"


def render_text(
    cells: list[dict[str, Any]],
    alpha_valid: frozenset[str] = ALPHA_VALID_CLASSES,
    outcome_valid: frozenset[str] = OUTCOME_VALID_CLASSES,
) -> str:
    lines: list[str] = []
    add = lines.append
    add("=" * 108)
    add(
        "\u6279\u6b21\u626b\u63cf —— \u6709\u6548\u6027\u6309「\u8981"
        "\u7b97\u54ea\u4e2a\u91cf」\u5b9a，\u4e0d\u662f\u6309 run \u5b9a（"
        "\u88c1\u51b3 2026-09-01）"
    )
    add(f"  α（\u59d4\u6d3e\u7387）\u5206\u6bcd valid \u7c7b: {sorted(alpha_valid)}")
    add(
        f"  resolve/\u6210\u672c \u5206\u6bcd"
        f" valid \u7c7b: {sorted(outcome_valid)}  （\u5176\u4f59\u4e3a\u53f3\u622a"
        f"\u5c3e，\u5355\u5217\u4e0d\u6df7"
        f"\u7b97）"
    )
    add("=" * 108)

    # ---- Exclusion ledger ------------------------------------------------
    # Printed first and unconditionally, including the zero case: without the
    # zero line "no dead runs" is indistinguishable from "never checked".
    add("")
    add(
        "--- \u5254\u9664\u62a5\u544a A：α \u7684\u5206\u6bcd（\u6bcf"
        "\u683c\u5fc5\u6253\u5370，\u96f6\u5254\u9664\u4e5f\u6253\u5370）-"
        "--"
    )
    for cell in cells:
        if cell["n_dropped_alpha"] == 0:
            add(
                f"{cell['cell']:<26s} \u5254\u9664 0 \u4e2a —— {cell['n_records']} \u4e2a run \u5168\u90e8\u8fdb α "
                f"\u5206\u6bcd"
            )
        else:
            breakdown = ", ".join(f"{k}={v}" for k, v in cell["dropped_alpha_by_class"].items())
            add(
                f"{cell['cell']:<26s} \u5254\u9664 {cell['n_dropped_alpha']} "
                + f"\u4e2a / \u5171 {cell['n_records']} \u4e2a ({breakdown})"
            )
            for bad in cell["dropped_alpha_runs"]:
                add(
                    f"{'':<26s}   - {bad['instance_id']:<38s} "
                    f"[{bad['validity_class']}] metrics.jsonl:{bad['metrics_line']} "
                    f"{bad['validity_evidence']}"
                )
    add(
        f"{_LABEL_TOTAL:<26s} \u5254\u9664 {sum(c['n_dropped_alpha'] for c in cells)} \u4e2a / \u5171 "
        f"{sum(c['n_records'] for c in cells)} \u4e2a run"
    )

    add("")
    add(
        "--- \u5254\u9664\u62a5\u544a B：resolve/\u6210\u672c \u7684"
        "\u5206\u6bcd（\u622a\u5c3e\u4e0e\u5254\u9664\u5206\u5f00\u62a5）-"
        "--"
    )
    for cell in cells:
        censored = ", ".join(f"{k}={v}" for k, v in cell["censored_outcome_by_class"].items())
        dropped = ", ".join(f"{k}={v}" for k, v in cell["dropped_outcome_by_class"].items())
        add(
            f"{cell['cell']:<26s} \u53ef\u7528 {cell['n_valid_outcome']}/{cell['n_records']}"
            f"；\u53f3\u622a\u5c3e {cell['n_censored_outcome']} \u4e2a"
            f"{' (' + censored + ')' if censored else ''}"
            f"；\u5254\u9664 {cell['n_dropped_outcome']} \u4e2a"
            f"{' (' + dropped + ')' if dropped else ''}"
        )
        for row in cell["censored_outcome_runs"]:
            add(
                f"{'':<26s}   ~ {row['instance_id']:<38s} [{row['validity_class']}] "
                f"token={row['tokens']} patch={row['patch_chars']} "
                f"(token \u662f\u4e0a\u9650\u4e0d"
                f"\u662f\u82b1\u8d39；patch \u975e"
                f"\u7a7a\u8bf4\u660e\u6d3b\u57fa"
                f"\u672c\u5e72\u5b8c\u4e86)"
            )

    # ---- Alpha table with both denominators ------------------------------
    add("")
    add(
        "--- \u59d4\u6d3e\u7387 α（\u65b0\u53e3\u5f84 = \u542b budget/tru"
        "ncated；\u65e7\u53e3\u5f84 = \u5168\u90e8 run）---"
    )
    add(f"{_LABEL_CELL:<26s} {_LABEL_ALPHA_DEN:>12s} {'α(valid)':>14s} {_LABEL_ALPHA_ALL:>16s}  \u53d8\u4e86?")
    for cell in cells:
        changed = (
            "\u53d8\u4e86"
            if (cell["alpha_den"] != cell["alpha_all_den"] or cell["alpha_num"] != cell["alpha_all_num"])
            else ""
        )
        add(
            f"{cell['cell']:<26s} "
            f"{str(cell['alpha_den']) + '/' + str(cell['n_records']):>12s} "
            f"{_fmt_rate(cell['alpha_num'], cell['alpha_den'], cell['alpha']):>14s} "
            f"{_fmt_rate(cell['alpha_all_num'], cell['alpha_all_den'], cell['alpha_all']):>16s}"
            f"  {changed}"
        )

    # ---- 3. outcome denominator, stated separately -----------------------
    add("")
    add(
        "--- resolve/\u6210\u672c \u53ef\u7528\u6837\u672c（\u53f3\u622a"
        "\u5c3e\u5355\u5217，\u7edd\u4e0d\u5e76\u5165\u5747\u503c）---"
    )
    add(
        f"{_LABEL_CELL:<26s} {_LABEL_UNCENSORED_DEN:>12s} {_LABEL_UNCENSORED_TOKENS:>28s} {_LABEL_CENSORED_TOKENS:>28s}"
    )
    for cell in cells:

        def _brief(values: list[Any]) -> str:
            nums = [v for v in values if isinstance(v, int | float)]
            if not nums:
                return "-"
            return f"n={len(nums)} \u5747\u503c={sum(nums) / len(nums):,.0f}"

        add(
            f"{cell['cell']:<26s} "
            f"{str(cell['n_valid_outcome']) + '/' + str(cell['n_records']):>12s} "
            f"{_brief(cell['tokens_outcome']):>28s} "
            f"{_brief(cell['tokens_censored']):>28s}"
        )

    # ---- 4. per-run detail ----------------------------------------------
    add("")
    add(
        "--- \u9010 run \u660e\u7ec6（A=\u8fdbα\u5206\u6bcd, O=\u8fdbreso"
        "lve/\u6210\u672c\u5206\u6bcd, ~=\u53f3\u622a\u5c3e, x=\u5254"
        "\u9664）---"
    )
    for cell in cells:
        for run in cell["runs"]:
            if run["valid_for_outcome"]:
                mark = "AO"
            elif run["censored_for_outcome"]:
                mark = "A~"
            else:
                mark = "xx"
            # By role, summed over the seats that held it, and only the roles
            # this run actually seated. Three fixed aids printed the wrong
            # column on every arm but the team one: a DW analyst is seated
            # twice (aids 0 and 3, so aid 3 fell off the end) and the single
            # arm's only seat is aid -1 (so all three columns read zero).
            seats = " ".join(f"{r}={n:,}" for r, n in run["assistant_msgs_by_role"].items()) or "-"
            tokens = " ".join(f"{r}={n:,}" for r, n in run["tokens_by_role"].items()) or "-"
            burnt = run.get("budget_exhausted_aids") or []
            burnt_note = f" \u70e7\u5149\u5ea7\u4f4d={burnt}" if burnt else ""
            add(
                f"[{mark}] {cell['cell']:<22s} {run['instance_id'][:34]:<34s} "
                f"{run['validity_class']:<19s} "
                f"\u5ea7\u4f4dmsg={seats:<28s} \u5ea7\u4f4dtoken={tokens:<48s} "
                f"msg_agent={run['message_agent_calls']:<3d} ts={run['team_status_calls']:<3d} "
                f"\u5199={str(run['write_tool_calls_by_aid']):<12s} "
                f"bash\u5199={str(run['bash_write_calls_by_aid']):<12s} "
                f"reason_chars={run['reasoning_chars_aid0']:<7d} "
                f"\u961f\u53cb\u8bcd={run['reasoning_teammate_hits']:<4d} "
                f"(\u6b63\u63a7 test/patch/fix={run['reasoning_control_hits']}) "
                f"patch={run['patch_chars']}{burnt_note}"
            )

    # ---- 5. which seat ran out -------------------------------------------
    add("")
    add(
        "--- \u9884\u7b97\u70e7\u5149\u53d1\u751f\u5728\u54ea\u4e2a"
        "\u5ea7\u4f4d（aid 0=analyst；analyst \u70e7\u5149\u4e0e coder "
        "\u70e7\u5149\u5bf9 α \u542b\u4e49\u76f8\u53cd）---"
    )
    any_seat = False
    for cell in cells:
        counts = cell["budget_exhausted_seat_counts"]
        if counts:
            any_seat = True
            add(f"{cell['cell']:<26s} {counts}")
    if not any_seat:
        add(
            "\u65e0\u5ea7\u4f4d\u8bb0\u5f55\u5230 budget \u7c7b terminal_rea"
            "son（\u6ce8\u610f：\u8fd9\u4e5f\u53ef\u80fd\u662f\u8be5\u5b57"
            "\u6bb5\u6ca1\u843d\u76d8，\u5c1a\u672a\u6838\u5bf9）"
        )

    # ---- 6. problems -----------------------------------------------------
    add("")
    add("--- \u89e3\u6790\u95ee\u9898（\u7a7a = \u65e0）---")
    any_problem = False
    for cell in cells:
        for problem in cell["problems"]:
            any_problem = True
            add(f"{cell['cell']:<26s} {problem}")
    if not any_problem:
        add("\u65e0")
    return "\n".join(lines)


CSV_FIELDS = (
    "cell",
    "instance_id",
    "metrics_line",
    "validity_class",
    "valid_for_alpha",
    "valid_for_outcome",
    "censored_for_outcome",
    "status",
    "reason",
    "steps",
    "tokens",
    "patch_chars",
    "message_agent_calls",
    "team_status_calls",
    "delegated",
    "reasoning_chars_aid0",
    "reasoning_teammate_hits",
    "reasoning_control_hits",
    "trajectory_found",
    "trajectory_dir",
)


def write_csv(path: str, cells: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDS), extrasaction="ignore")
        writer.writeheader()
        for cell in cells:
            for run in cell["runs"]:
                writer.writerow(run)


def build_document(
    cells: list[dict[str, Any]],
    alpha_valid: frozenset[str] = ALPHA_VALID_CLASSES,
    outcome_valid: frozenset[str] = OUTCOME_VALID_CLASSES,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        # Two denominators, named, so a downstream reader can never pick one up
        # thinking it is the other.
        "alpha_valid_classes": sorted(alpha_valid),
        "outcome_valid_classes": sorted(outcome_valid),
        "censored_for_outcome_classes": sorted(alpha_valid - outcome_valid),
        "criterion_verified_against_real_records": False,
        "criterion_verification_note": (
            "Source-derived from the OpenCollab / OpenCollab-Eval writers and "
            "fixture-tested only; gpu3 was unreachable. Re-run the positive "
            "control on think-decide-first before publishing any number. "
            "Validity is per-quantity (coordinator ruling 2026-09-01): "
            "budget_exhausted is valid for alpha and censored for resolve/cost."
        ),
        "totals": {
            "n_records": sum(c["n_records"] for c in cells),
            "n_valid_alpha": sum(c["n_valid_alpha"] for c in cells),
            "n_dropped_alpha": sum(c["n_dropped_alpha"] for c in cells),
            "n_valid_outcome": sum(c["n_valid_outcome"] for c in cells),
            "n_censored_outcome": sum(c["n_censored_outcome"] for c in cells),
        },
        "cells": [{k: v for k, v in c.items() if k != "runs"} for c in cells],
        "runs": [r for c in cells for r in c["runs"]],
    }


# =============================================================================
# DEFERRED VERIFICATION -- run this the moment gpu3 is reachable again.
# =============================================================================
DEFERRED_VERIFICATION = (
    "\ngpu3 was down when this scanner was written, so the validity "
    "criterion above is\nsource-derived and fixture-tested only.  No"
    "thing from it should go in the paper\nuntil this positive contr"
    "ol passes.\n\nWhy this cell and not a clean one: a clean batch "
    'cannot tell you whether the\ndead-run detector works, because "'
    'no dead runs" and "detector broken" produce\nidentical output t'
    "here.  think-decide-first is the one cell known to contain\ndea"
    "d runs, so it is the only cell where a pass means anything.\n\n"
    "  # 1. copy the scanner over (it is stdlib-only, no install nee"
    "ded)\n  scp oc-iclr2027/05-experiment/scripts/scan_batch.py gpu"
    "3:/tmp/scan_batch.py\n\n  # 2. the positive control\n  ssh gpu3"
    " 'python3 /tmp/scan_batch.py ~/oc-team-smoke/think-decide-first"
    "'\n\nEXPECT, and treat any deviation as the measurement winning"
    ' over this comment:\n  * section A: "\u5254\u9664 2 \u4e2a / '
    '\u5171 3 \u4e2a (api_error=2)"\n  * the two named runs are the '
    "ones at metrics.jsonl lines 2 and 3\n  * the alpha row reads  1"
    "/3  ...  alpha(valid) = 0/1\n    beside alpha(\u65e7, \u5168"
    "\u90e8) = 0/3, flagged \u53d8\u4e86\n  * section B: the same ce"
    "ll shows \u53ef\u7528 1/3, \u53f3\u622a\u5c3e 0, \u5254\u9664 2"
    "\n\nAlso check the seat readout on a cell that DID hit its cap"
    "\n(think-cmd-optout / think-opt-out-message / starved-nudgeoff,"
    ' all on\npydicom-1031).  Expect "\u9884\u7b97\u70e7\u5149\u53d1'
    '\u751f\u5728\u54ea\u4e2a\u5ea7\u4f4d" to name aid 0, and expect'
    " the\ncensored rows to show a non-empty patch (~1113 / 1091 / 1"
    '097 bytes).  If the\nseat line instead says "\u65e0\u5ea7\u4f4d'
    '\u8bb0\u5f55\u5230 budget \u7c7b terminal_reason", find out whe'
    "ther\nsession_state.terminal_reason is actually being persisted"
    " before concluding\nanything from it -- an empty readout there "
    "is not evidence of no burnout.\n  * the printed reason string f"
    "or both is checked by eye against\n    ~/oc-team-smoke/think-de"
    "cide-first/logs-team/pydicom__pydicom-1031/driver.log\n\nIF THE"
    " REASON STRINGS DIFFER from the patterns in _RULES, do not wide"
    "n a\npattern to make the number come out right.  Print the raw "
    "values first:\n\n  ssh gpu3 'python3 -c \"\nimport json,sys\nfo"
    "r i,l in enumerate(open(sys.argv[1]),1):\n    d=json.loads(l); "
    'r=d.get("run_summary") or {}\n    print(i, d.get("instance_id")'
    ', repr(r.get("status")), repr(r.get("reason")), repr(r.get("err'
    'or"))[:200])\n" ~/oc-team-smoke/think-decide-first/metrics.json'
    "l'\n\n  # and confirm the enumeration across every cell at once"
    ", so an unseen\n  # runtime string shows up as `unclassified` r"
    "ather than as a shifted rate:\n  ssh gpu3 'python3 /tmp/scan_ba"
    "tch.py ~/oc-team-smoke/think-*       ~/oc-team-smoke/starved-nu"
    "dge* ~/oc-team-smoke/facts-v2-rep*       --json /tmp/scan.json'"
    " | tail -40\n\nThen set criterion_verified_against_real_records"
    " to True in build_document, and\nonly then quote a number.\n"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan experiment batch cells; report discarded runs explicitly.")
    parser.add_argument("batch_dirs", nargs="+", help="one or more cell directories")
    parser.add_argument("--json", dest="json_out", help="write the machine-readable JSON here")
    parser.add_argument("--csv", dest="csv_out", help="write one row per run here")
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "sensitivity check in the conservative direction: also drop "
            "budget_exhausted and provider_truncated from the alpha denominator, "
            "making it identical to the resolve/cost denominator"
        ),
    )
    parser.add_argument(
        "--treat-valid",
        default="",
        help=(
            "comma-separated validity classes to additionally count in the alpha "
            "denominator, for sensitivity in the permissive direction"
        ),
    )
    args = parser.parse_args(argv)

    extra = {c.strip() for c in args.treat_valid.split(",") if c.strip()}
    alpha_valid = frozenset((OUTCOME_VALID_CLASSES if args.strict else ALPHA_VALID_CLASSES) | extra)
    outcome_valid = OUTCOME_VALID_CLASSES

    cells = [scan_cell(d, alpha_valid, outcome_valid) for d in args.batch_dirs]
    print(render_text(cells, alpha_valid, outcome_valid))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(
                build_document(cells, alpha_valid, outcome_valid),
                handle,
                ensure_ascii=False,
                indent=2,
            )
        print(f"\nJSON -> {args.json_out}")
    if args.csv_out:
        write_csv(args.csv_out, cells)
        print(f"CSV  -> {args.csv_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
