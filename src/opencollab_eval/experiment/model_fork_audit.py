"""Read-only gate over every fork keyed on the model name.

    python -m opencollab_eval.experiment.model_fork_audit qwen3.8-flash

Prints one row per fork -- quantity, ``path:line``, resolved value, and whether
that value was reached by a row written for this model or by a default nobody
chose for it -- and exits non-zero on three things:

``fallback``
    A quantity landed on a module constant or a family row instead of an exact
    one. This is the whole family of defects: every lookup here answers
    *something* for an unknown model, so the first batch on a new model runs on
    a window, a compaction trigger and a tool-choice policy that were never
    checked against it. ``--no-fail-on-fallback`` turns the gate off.
``cross-repo``
    OpenCollab and OpenCollab-Eval resolved the same quantity differently. This
    is the one that matters most: the runtime enforces its number and the
    recorder writes down its own, so the audit table reports a context window
    the run never had. ``--no-fail-on-cross-repo`` turns it off.
``declaration``
    Optional. ``--declare FILE`` names the values a batch is supposed to run
    under; anything resolved differently fails. ``--no-fail-on-declaration``
    turns it off.

Two states are reported but not failed by default, because both are choices
somebody made rather than accidents:

* ``row-default`` -- the model has a row and the row leaves this field out, so
  the dataclass default applies. ``--fail-on-row-default`` makes it fail.
* ``pricing.mode = unset`` -- every batch on record priced tokens from the
  environment rather than a table, so failing it would fail every model.

This tool writes nothing, starts nothing, and reaches no network unless
``--probe`` is passed (see ``model_fork_probe``).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from opencollab_eval.experiment.model_forks import (
    DEFAULT_OVERFLOW_SAMPLES,
    DEFAULT_RETRY_SAMPLES,
    FALLBACK,
    FAMILY,
    GATE_CROSS_REPO,
    GATE_FALLBACK,
    ROW_DEFAULT,
    ErrorSample,
    Finding,
    ForkResolutionError,
    resolve,
)

OK = "ok"
FAIL = "FAIL"
NOTE = "note"

#: Gate names, in the order they are reported.
GATES = ("fallback", "cross-repo", "declaration", "row-default")


def load_mapping(path: Path) -> dict[str, Any]:
    """Read a declaration or sample file. YAML when available, else JSON."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - yaml ships with the repo
            raise ForkResolutionError(f"{path} is YAML but PyYAML is not installed") from exc
        loaded = yaml.safe_load(text)
    else:
        loaded = json.loads(text)
    if not isinstance(loaded, dict):
        raise ForkResolutionError(f"{path} must hold a mapping at the top level")
    return loaded


def samples_from(mapping: dict[str, Any], key: str, default: tuple[ErrorSample, ...]) -> tuple[ErrorSample, ...]:
    rows = mapping.get(key)
    if rows is None:
        return default
    if not isinstance(rows, list):
        raise ForkResolutionError(f"'{key}' must be a list of samples")
    return tuple(
        ErrorSample(
            label=str(row["label"]),
            message=str(row["message"]),
            status=row.get("status"),
            class_name=str(row.get("class_name", "APIStatusError")),
        )
        for row in rows
    )


def _key(finding: Finding) -> str:
    return finding.quantity if finding.side == "opencollab" else f"{finding.side}:{finding.quantity}"


def verdicts(
    findings: list[Finding],
    *,
    fail_on_fallback: bool = True,
    fail_on_cross_repo: bool = True,
    fail_on_row_default: bool = False,
) -> dict[int, tuple[str, str]]:
    """``{index: (verdict, reason)}`` for each finding, under the active gates."""
    out: dict[int, tuple[str, str]] = {}
    for index, finding in enumerate(findings):
        verdict, reason = OK, ""
        if finding.gate == GATE_FALLBACK and finding.hit in {FALLBACK, FAMILY}:
            reason = (
                f"landed on {finding.value!r} by {finding.hit}, not by a row for this model"
            )
            verdict = FAIL if fail_on_fallback else NOTE
        elif finding.hit == ROW_DEFAULT:
            reason = f"dataclass default {finding.fallback_value!r}; the row does not set it"
            verdict = FAIL if fail_on_row_default else NOTE
        elif finding.gate == GATE_CROSS_REPO and finding.hit == "DISAGREE":
            reason = "the two repositories resolve this to different values"
            verdict = FAIL if fail_on_cross_repo else NOTE
        elif finding.hit == FALLBACK:
            reason = f"default {finding.fallback_value!r} (not gated)"
            verdict = NOTE
        out[index] = (verdict, reason)
    return out


def declaration_failures(findings: list[Finding], declared: dict[str, Any]) -> list[str]:
    """Every declared value the code does not agree with, plus every typo.

    An unknown key fails on purpose: a declaration whose key never matched
    anything would pass forever while checking nothing, which is the same shape
    of defect as the silent fallbacks this tool is about.
    """
    resolved = {_key(finding): finding.value for finding in findings if finding.side != "cross-repo"}
    failures: list[str] = []
    for key, expected in sorted(declared.items()):
        if key not in resolved:
            failures.append(f"{key}: declared, but this tool resolves no such quantity (typo?)")
        elif resolved[key] != expected:
            failures.append(f"{key}: declared {expected!r}, resolved {resolved[key]!r}")
    return failures


def render(model: str, findings: list[Finding], marks: dict[int, tuple[str, str]]) -> str:
    header = ("QUANTITY", "SIDE", "HIT", "VALUE", "VERDICT", "SOURCE")
    rows = [
        (
            finding.quantity,
            finding.side,
            finding.hit,
            repr(finding.value) if not isinstance(finding.value, str) else finding.value,
            marks[index][0],
            finding.source,
        )
        for index, finding in enumerate(findings)
    ]
    widths = [max(len(row[column]) for row in (header, *rows)) for column in range(len(header))]
    lines = [f"model: {model}", ""]
    lines.append("  ".join(cell.ljust(width) for cell, width in zip(header, widths, strict=True)).rstrip())
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="model_fork_audit",
        description="Resolve every model-keyed fork in OpenCollab and OpenCollab-Eval for one model.",
    )
    parser.add_argument("model", help="Model identifier exactly as a batch spec sets it")
    parser.add_argument("--declare", type=Path, help="YAML/JSON file of the values this model is supposed to have")
    parser.add_argument("--samples", type=Path, help="YAML/JSON file overriding the replayed provider errors")
    parser.add_argument("--json", action="store_true", help="Emit findings as JSON instead of a table")
    parser.add_argument("--no-fail-on-fallback", action="store_true", help="Report silent fallbacks without failing")
    parser.add_argument("--no-fail-on-cross-repo", action="store_true", help="Report cross-repo drift without failing")
    parser.add_argument(
        "--no-fail-on-declaration", action="store_true", help="Report declaration drift without failing"
    )
    parser.add_argument(
        "--fail-on-row-default",
        action="store_true",
        help="Also fail when a model's row exists but leaves a capability field at its dataclass default",
    )
    parser.add_argument(
        "--probe",
        help=(
            "Comma-separated endpoint probes to run against the live model "
            "(context,tool_choice,max_tokens). Costs money; off by default."
        ),
    )
    parser.add_argument(
        "--probe-max-requests", type=int, default=10, help="Hard ceiling on probe requests (default 10)"
    )
    parser.add_argument("--probe-dry-run", action="store_true", help="Plan the probes and print the cost, send nothing")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overflow, retries = DEFAULT_OVERFLOW_SAMPLES, DEFAULT_RETRY_SAMPLES
    if args.samples:
        mapping = load_mapping(args.samples)
        overflow = samples_from(mapping, "overflow", overflow)
        retries = samples_from(mapping, "retry", retries)

    findings = resolve(args.model, overflow_samples=overflow, retry_samples=retries)
    marks = verdicts(
        findings,
        fail_on_fallback=not args.no_fail_on_fallback,
        fail_on_cross_repo=not args.no_fail_on_cross_repo,
        fail_on_row_default=args.fail_on_row_default,
    )

    declared: dict[str, Any] = {}
    if args.declare:
        document = load_mapping(args.declare)
        model_field = document.get("model")
        if model_field is not None and model_field != args.model:
            raise ForkResolutionError(f"{args.declare} declares model {model_field!r}, audited {args.model!r}")
        declared = document.get("expect") or {}
        if not isinstance(declared, dict):
            raise ForkResolutionError(f"{args.declare}: 'expect' must be a mapping")
    declared_failures = declaration_failures(findings, declared) if declared else []

    probe_report: dict[str, Any] | None = None
    if args.probe or args.probe_dry_run:
        from opencollab_eval.experiment import model_fork_probe

        probe_report = model_fork_probe.run_probes(
            args.model,
            requested=args.probe or "context,tool_choice,max_tokens",
            max_requests=args.probe_max_requests,
            dry_run=args.probe_dry_run,
        )

    failures = [_key(findings[index]) for index, (verdict, _) in marks.items() if verdict == FAIL]
    if declared_failures and not args.no_fail_on_declaration:
        failures.extend(declared_failures)

    if args.json:
        print(json.dumps(
            {
                "model": args.model,
                "findings": [
                    {**asdict(finding), "verdict": marks[index][0], "reason": marks[index][1]}
                    for index, finding in enumerate(findings)
                ],
                "declaration_failures": declared_failures,
                "probe": probe_report,
                "failed": bool(failures),
            },
            indent=2,
            default=str,
        ))
        return 1 if failures else 0

    print(render(args.model, findings, marks))
    print()
    for index, finding in enumerate(findings):
        verdict, reason = marks[index]
        if verdict != OK:
            print(f"{verdict:4}  {_key(finding):45} {reason}")
            if finding.note:
                print(f"      {'':45} {finding.note}")
    for failure in declared_failures:
        print(f"FAIL  declaration: {failure}")
    if probe_report is not None:
        print()
        print(json.dumps(probe_report, indent=2, default=str))
    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) -- {', '.join(sorted(set(failures))[:6])}")
        return 1
    print("PASSED: every gated quantity resolved from a row written for this model, "
          "and the two repositories agree")
    return 0


def main() -> int:
    try:
        return run()
    except ForkResolutionError as error:
        print(f"model_fork_audit: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - manual inspection entry point
    raise SystemExit(main())
