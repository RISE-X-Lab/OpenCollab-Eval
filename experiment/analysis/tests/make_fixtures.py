#!/usr/bin/env python3
"""Build the synthetic fixture cells used by test_scan_batch.py.

Every ``run_summary`` status/reason pair below is copied verbatim from the
writer-side source (see the provenance block in scan_batch.py), so the fixtures
exercise the real string shapes rather than invented ones.  They are still
synthetic: they prove the classifier handles these shapes, not that these are
the shapes gpu3 actually wrote.
"""
from __future__ import annotations

import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "fixtures")


def agent(aid, role, used_tokens, steps, messages, phase="done", terminal_reason=None):
    return {
        "snapshot_version": 1,
        "aid": aid,
        "role": role,
        "model": "deepseek-v4-flash",
        "session_state": {
            "used_tokens": used_tokens,
            "context_tokens": 1234,
            "step_count": steps,
            "phase": phase,
            "terminal_reason": terminal_reason,
        },
        "messages": messages,
    }


def tool_call(name, arguments):
    return {"type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def assistant(reasoning=None, calls=()):
    msg = {"role": "assistant", "content": ""}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    if calls:
        msg["tool_calls"] = list(calls)
    return msg


def write_run(cell_dir, instance_id, agents):
    runtime = os.path.join(
        cell_dir, "logs-team", instance_id, "trajectories", "t0", "runtime-deadbeef"
    )
    os.makedirs(runtime, exist_ok=True)
    with open(os.path.join(runtime, "team.json"), "w", encoding="utf-8") as fh:
        json.dump({"team_file": "team.handoff.experiment.yaml"}, fh)
    for a in agents:
        path = os.path.join(runtime, f"agent_{a['aid']}_{a['role']}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(a, fh)


def metrics_row(instance_id, status, reason, steps, tokens, patch_chars, error=None):
    return {
        "instance_id": instance_id,
        "submitted_patch_chars": patch_chars,
        "run_summary": {
            "steps": steps,
            "tokens": tokens,
            "status": status,
            "reason": reason,
            "duration_s": 12.5,
            "error": error,
        },
    }


def build():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)

    # ---------------- cell A: one of each disposition --------------------
    cell = os.path.join(ROOT, "cell-mixed")
    os.makedirs(cell, exist_ok=True)
    rows = []

    # 1. VALID, and it delegated.  Numerator of alpha.
    rows.append(metrics_row("acme__acme-1", "completed", None, 30, 900_000, 1500))
    write_run(cell, "acme__acme-1", [
        agent(0, "analyst", 500_000, 20, [
            assistant(
                reasoning="I should hand this to the coder; delegate the edit.",
                calls=[tool_call("message_agent", {"to_role": "coder", "content": "please patch"}),
                       tool_call("team_status", {})],
            ),
            assistant(reasoning="Now run the test suite to verify the patch and fix it."),
        ]),
        # This seat also has reasoning text, and it also says "coder".  If the
        # scanner ever counts reasoning across all seats, both the aid=0
        # character count and the teammate-term count move.
        agent(1, "coder", 300_000, 8, [
            assistant(
                reasoning="As the coder I apply the patch the coder was asked for.",
                calls=[tool_call("apply_patch", {"patch": "x"})],
            ),
        ]),
        agent(2, "tester", 100_000, 2, [
            assistant(calls=[tool_call("bash", {"command": "pytest -q > /tmp/out.txt"})]),
        ]),
    ])

    # 2. INVALID api_error -- but it DID call message_agent.  If invalid runs
    #    leak into alpha, both numerator and denominator move, so the test
    #    below discriminates.  This is the shape of think-decide-first.
    rows.append(metrics_row(
        "acme__acme-2", "failed",
        "APIError: Upstream response stream was interrupted",
        22, 410_000, 0,
        error="Upstream response stream was interrupted",
    ))
    write_run(cell, "acme__acme-2", [
        agent(0, "analyst", 410_000, 22, [
            assistant(reasoning="delegate to coder now",
                      calls=[tool_call("message_agent", {"to_role": "coder", "content": "go"})]),
        ], phase="error"),
    ])

    # 3. budget_exhausted: VALID for alpha (it had every chance and did not
    #    delegate), CENSORED for resolve/cost.  Patch size copied from the real
    #    cap-hitting runs (1113 / 1091 / 1097 bytes): the work was largely done.
    #    The ANALYST seat is the one that ran out here.
    rows.append(metrics_row(
        "acme__acme-3", "stopped", "budget exceeded: 2000000 tokens used",
        60, 2_000_000, 1113,
    ))
    write_run(cell, "acme__acme-3", [
        agent(0, "analyst", 2_000_000, 60, [assistant(reasoning="keep going, fix the test")],
              phase="stopped", terminal_reason="budget exceeded: 2000000 tokens used"),
        agent(1, "coder", 0, 0, []),
    ])

    # 4. provider_truncated: VALID for alpha, CENSORED for resolve/cost.
    rows.append(metrics_row(
        "acme__acme-4", "stopped",
        "output truncated: provider reached its generation limit",
        11, 120_000, 1091,
    ))
    write_run(cell, "acme__acme-4", [
        agent(0, "analyst", 120_000, 11, [assistant(reasoning="patch the file")],
              phase="stopped",
              terminal_reason="output truncated: provider reached its generation limit"),
    ])

    # 5. harness_error: INVALID everywhere (our own lifecycle fault).
    rows.append(metrics_row(
        "acme__acme-5", "failed",
        "ProgrammaticLifecycleError: agent trajectory persistence failed",
        0, 0, 0,
        error="agent trajectory persistence failed",
    ))
    write_run(cell, "acme__acme-5", [agent(0, "analyst", 0, 0, [])])

    # 6. budget_exhausted again, but the CODER seat is the one that ran out,
    #    after a handover that did happen.  Same run-level label as #3 and the
    #    opposite meaning for alpha -- which is why the seat is read separately.
    rows.append(metrics_row(
        "acme__acme-6", "stopped", "budget_exceeded", 55, 1_900_000, 1097,
    ))
    write_run(cell, "acme__acme-6", [
        agent(0, "analyst", 900_000, 20, [
            assistant(reasoning="hand off to the coder, then test the fix",
                      calls=[tool_call("message_agent", {"to_role": "coder", "content": "go"})]),
        ]),
        agent(1, "coder", 1_000_000, 35, [
            assistant(calls=[tool_call("apply_patch", {"patch": "y"})]),
        ], phase="stopped", terminal_reason="budget exceeded: 1000000 tokens used"),
    ])

    # 7. a second api_error that also delegated, so the old all-runs alpha and
    #    the new one differ in VALUE and not only in denominator.
    rows.append(metrics_row(
        "acme__acme-7", "failed",
        "APIError: Upstream response stream was interrupted",
        11, 200_000, 0,
        error="Upstream response stream was interrupted",
    ))
    write_run(cell, "acme__acme-7", [
        agent(0, "analyst", 200_000, 11, [
            assistant(reasoning="delegate this",
                      calls=[tool_call("message_agent", {"to_role": "coder", "content": "go"})]),
        ], phase="error"),
    ])

    with open(os.path.join(cell, "metrics.jsonl"), "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    # ---------------- cell B: nothing to discard -------------------------
    clean = os.path.join(ROOT, "cell-clean")
    os.makedirs(clean, exist_ok=True)
    clean_rows = [
        metrics_row("beta__beta-1", "completed", None, 25, 700_000, 1200),
        metrics_row("beta__beta-2", "completed", None, 31, 810_000, 950),
    ]
    for r in clean_rows:
        write_run(clean, r["instance_id"], [
            agent(0, "analyst", 700_000, 25, [assistant(reasoning="fix the test myself")]),
        ])
    with open(os.path.join(clean, "metrics.jsonl"), "w", encoding="utf-8") as fh:
        for row in clean_rows:
            fh.write(json.dumps(row) + "\n")

    # ---------------- cell C: an unseen reason string --------------------
    weird = os.path.join(ROOT, "cell-unknown")
    os.makedirs(weird, exist_ok=True)
    weird_rows = [
        metrics_row("gamma__gamma-1", "stopped", "a reason no version of the runtime has ever emitted", 5, 100, 0),
    ]
    for r in weird_rows:
        write_run(weird, r["instance_id"], [agent(0, "analyst", 100, 5, [])])
    with open(os.path.join(weird, "metrics.jsonl"), "w", encoding="utf-8") as fh:
        for row in weird_rows:
            fh.write(json.dumps(row) + "\n")

    print("fixtures ->", ROOT)


if __name__ == "__main__":
    build()
