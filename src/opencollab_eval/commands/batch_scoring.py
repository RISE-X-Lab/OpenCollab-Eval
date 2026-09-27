"""Remote official grading commands for a resolved batch."""

from __future__ import annotations

import json
import shlex
import time
from typing import TYPE_CHECKING, Any

from opencollab_eval.experiment import batch_remote, batch_score
from opencollab_eval.safe_files import write_regular_bytes_atomic

if TYPE_CHECKING:
    from opencollab_eval.commands.batch import Batch, Ssh


def _score_facts(batch: Batch, remote: Ssh) -> dict[str, Any]:
    facts = batch_remote.parse_facts(remote.run(batch_score.score_guard_script(batch.host, batch.spec)))
    out: dict[str, Any] = {"RUNNING": [f[1] for f in facts if f[0] == "RUNNING" and len(f) > 1]}
    for fact in facts:
        if fact[0] != "RUNNING" and len(fact) > 1:
            out[fact[0]] = fact[1]
    return out


def cmd_score(batch: Batch, remote: Ssh, *, max_workers: int, timeout: int, gold: bool) -> int:
    """Score a finished batch: the gold control first, then the batch's patches.

    Blocking, and slow -- the harness builds a container per instance. Run it
    detached and read the printed report paths afterwards.
    """
    spec, host = batch.spec, batch.host
    ids = [row["instance_id"] for row in batch.rows]
    print(f"score {spec.name} on {host.ssh}: {len(ids)} instances, arm {spec.arm}")

    if not host.scoring_dataset:
        print("  REFUSED: this host file names no scoring_dataset. Nothing was run.")
        return 1
    facts = _score_facts(batch, remote)
    if facts.get("DECOY_HIT", "0") == "0":
        print("  REFUSED: the running-scorer check found not even its own decoy, so a quiet")
        print("           result from it means nothing. Nothing was run.")
        return 1
    if facts["RUNNING"]:
        print(f"  REFUSED: {len(facts['RUNNING'])} harness process(es) already running on this host.")
        for line in facts["RUNNING"]:
            print(f"           {line}")
        return 1
    checks = [
        batch_remote.Check("predictions file", facts.get("PREDS") == "present", batch_score.preds_path(host, spec)),
        batch_remote.Check(
            "one prediction per instance",
            facts.get("PREDS_ROWS") == str(len(ids)),
            f"{facts.get('PREDS_ROWS', '0')} rows of {len(ids)} instances",
        ),
        # An empty patch is not an error, but it is not a scored attempt
        # either: it resolves nothing and is indistinguishable in the report
        # from a patch that was tried and failed.
        batch_remote.Check(
            "predictions carrying a patch",
            True,
            f"{int(facts.get('PREDS_ROWS', 0) or 0) - int(facts.get('PREDS_EMPTY', 0) or 0)}"
            f" of the {facts.get('PREDS_ROWS', '0')} rows present",
            warn=True,
        ),
        batch_remote.Check(
            "swebench importable",
            facts.get("SWEBENCH", "absent") not in ("absent", "unimportable"),
            f"swebench {facts.get('SWEBENCH', 'absent')}",
        ),
        batch_remote.Check(
            "disk free",
            float(facts.get("DISK_FREE_GB", 0) or 0) >= host.min_free_gb,
            f"{facts.get('DISK_FREE_GB', '?')} GB free, need {host.min_free_gb:g}",
        ),
    ]
    from opencollab_eval.commands.batch import _print_checks

    if not _print_checks(checks):
        print("RESULT: not scored")
        return 1

    dataset_facts = batch_remote.parse_facts(remote.run(batch_score.dataset_script(host, spec, ids), timeout=600))
    made = {f[0]: f[1] for f in dataset_facts if len(f) > 1}
    if "DATASET_MISSING" in made:
        print(f"  REFUSED: the host's dataset has no row for {made['DATASET_MISSING']}. Nothing was run.")
        return 1
    print(f"  dataset: {made.get('DATASET_ROWS', '?')} rows -> {batch_score.score_dir(host, spec)}/dataset.jsonl")

    sdir = batch_score.score_dir(host, spec)
    stamp = time.strftime("%Y%m%d")
    if gold:
        rows = batch_score.dataset_rows(batch.frame_content, ids)
        gold_file = batch.local_dir / "gold-predictions.jsonl"
        write_regular_bytes_atomic(
            gold_file, "".join(json.dumps(r) + "\n" for r in batch_score.gold_predictions(rows)).encode("utf-8")
        )
        remote.copy_to([gold_file], sdir)
        gold_id = f"{spec.name}-gold-{stamp}"
        print(f"  gold control: {gold_id} ({len(rows)} reference patches)")
        remote.run(
            batch_score.score_script(
                host,
                spec,
                run_id=gold_id,
                predictions=f"{sdir}/gold-predictions.jsonl",
                dataset=f"{sdir}/dataset.jsonl",
                max_workers=max_workers,
                timeout=timeout,
            ),
            timeout=600,
        )
        print("  gold run launched. It must resolve every instance before any rate from")
        print("  this host is readable, so it runs alone and the batch's own patches wait:")
        print(f"    batch score-report {batch.spec_path}      # when it finishes, the verdict")
        print(f"    batch score --no-gold {batch.spec_path}   # then the batch's patches")
        return 0

    run_id = f"{spec.name}-{stamp}"
    launched = remote.run(
        batch_score.score_script(
            host,
            spec,
            run_id=run_id,
            predictions=batch_score.preds_path(host, spec),
            dataset=f"{sdir}/dataset.jsonl",
            max_workers=max_workers,
            timeout=timeout,
        ),
        timeout=600,
    )
    print(f"  launched {run_id}: {launched.strip()[:160] or '(no matching process)'}")
    return 0


def cmd_score_report(batch: Batch, remote: Ssh) -> int:
    """Read this batch's scoring reports off the host and print what they say.

    The gold control is read first and its verdict gates the rest: a session
    whose reference patches do not all resolve cannot judge ours, and the
    resolve rate it returns is a statement about the machine.
    """
    spec, host = batch.spec, batch.host
    sdir = batch_score.score_dir(host, spec)
    listing = remote.run(f"ls -1 {shlex.quote(sdir + '/' + batch_score.REPORT_DIR)} 2>/dev/null || true")
    names = [n.strip() for n in listing.splitlines() if n.strip().endswith(".json")]
    if not names:
        print(f"no reports under {sdir}/{batch_score.REPORT_DIR} yet")
        return 1
    gold_ok = None
    for name in sorted(names):
        text = remote.run(f"cat {shlex.quote(sdir + '/' + batch_score.REPORT_DIR + '/' + name)}")
        report = batch_score.read_report(text)
        if "gold" in name:
            gold_ok, detail = batch_score.gold_verdict(report)
            print(f"  [{'ok  ' if gold_ok else 'FAIL'}] {name}: {detail}")
            continue
        print(
            f"  {name}: resolved {report['resolved']}/{report['total']}"
            f" (completed {report['completed']}, empty patch {len(report['empty_patch_ids'])},"
            f" errors {len(report['error_ids'])})"
        )
    if gold_ok is None:
        print("  no gold control in this session: every rate above is unverified.")
        return 1
    if not gold_ok:
        print("  the control failed, so no rate above is readable as a result.")
        return 1
    return 0


# --- entry ---------------------------------------------------------------------
