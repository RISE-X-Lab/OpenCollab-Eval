"""Scoring a finished batch: the gold control first, then the batch's patches.

The chain this replaces was three hand-written shell scripts per scoring
session, each naming its host directory, its dataset file and its run id as
literals. They worked; what they could not do is say, afterwards, which
dataset a number came from. This module builds the same commands from the
batch spec, so the scoring of a batch is addressed the same way the batch is.

Two things here are not conveniences.

The **gold control runs first and its result gates the rest.** On 2026-09-07 a
cell reported one task as unresolvable with a note about a bad evaluation
environment; the environment was a machine with no route to the package
index, and the same task's own reference patch resolves on a machine that has
one. A scoring run that cannot resolve the reference patch cannot say
anything about ours, and the only way to notice is to score the reference
patch in the same session, on the same host, against the same dataset.

The **predictions file is counted before it is scored.** A batch whose driver
died mid-way leaves a short predictions file, and a short file scores without
complaint: the instances that are missing are simply not in the report, and
the resolve rate that comes back is a rate over the runs that happened to
finish. The row count and the patched count are read out and compared against
the spec's instance list before anything is launched.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from opencollab_eval.experiment.batch_spec import BatchSpec, HostConfig

#: The harness writes one report per (model, run id) pair under this directory.
REPORT_DIR = "reports"

#: A decoy whose command line carries the pattern the running-check looks for,
#: so a check that has stopped matching anything says so rather than reading
#: as "no scoring is running".
SCORE_DECOY_MARK = "swebench.harness.run_evaluation decoy-for-score"

#: What the harness process looks like on the host.
SCORE_PROCESS_PATTERN = "swebench.harness.run_evaluation"


def _q(value: str) -> str:
    return shlex.quote(value)


def score_dir(host: HostConfig, spec: BatchSpec) -> str:
    """Where one batch's scoring session keeps its files on the host."""
    return f"{host.workdir}/oc-scoring/{spec.name}"


def preds_path(host: HostConfig, spec: BatchSpec) -> str:
    """The predictions file the batch driver already wrote, in the out-dir.

    The driver names it after the arm, not after the batch, because one
    out-dir holds one arm. It is read where it lies rather than copied: a
    second copy is a second thing that can be stale.
    """
    return f"{host.workdir}/{spec.name}/preds-{spec.arm}.jsonl"


def gold_predictions(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The reference patches, in the shape the harness reads predictions in.

    This is the positive control: every one of these must resolve. A run in
    which they do not is a broken environment, and no number from it means
    what it says.
    """
    return [
        {
            "instance_id": row["instance_id"],
            "model_name_or_path": "gold",
            "model_patch": row["patch"],
        }
        for row in rows
    ]


def dataset_rows(frame_content: str | Path, instance_ids: list[str]) -> list[dict[str, Any]]:
    """The frame's rows for these instances, in the frame's own order.

    Raises if any instance is missing rather than scoring a smaller dataset:
    a dataset that silently drops rows produces a report whose denominator is
    not the batch's.
    """
    wanted = set(instance_ids)
    found: dict[str, dict[str, Any]] = {}
    with Path(frame_content).open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("instance_id") in wanted:
                found[row["instance_id"]] = row
    missing = [i for i in instance_ids if i not in found]
    if missing:
        raise ValueError(f"frame has no row for {len(missing)} instances: {missing[:5]}")
    return [found[i] for i in instance_ids]


def scoring_python(host: HostConfig) -> str:
    """The interpreter the harness runs under on the host."""
    return host.scoring_python or f"{host.workdir}/oc-scoring/.venv-swebench/bin/python"


def dataset_script(host: HostConfig, spec: BatchSpec, instance_ids: list[str]) -> str:
    """Cut the host's full dataset down to this batch's instances.

    The filtering happens on the host because the dataset is a hundred
    megabytes of eval scripts and the instance list is fifty strings. It
    refuses rather than writing a shorter file: a dataset missing rows scores
    without complaint and returns a rate whose denominator is not the batch's.
    """
    sdir = score_dir(host, spec)
    source = host.scoring_dataset or ""
    ids = json.dumps(instance_ids)
    body = (
        "import json,sys\n"
        "want=json.loads(sys.argv[2])\n"
        "rows={}\n"
        "for line in open(sys.argv[1],encoding='utf-8'):\n"
        "    line=line.strip()\n"
        "    if not line: continue\n"
        "    r=json.loads(line)\n"
        "    if r.get('instance_id') in want: rows[r['instance_id']]=r\n"
        "missing=[i for i in want if i not in rows]\n"
        "if missing:\n"
        "    print('DATASET_MISSING\\t'+','.join(missing[:8])); raise SystemExit(0)\n"
        "with open(sys.argv[3],'w',encoding='utf-8') as out:\n"
        "    for i in want: out.write(json.dumps(rows[i])+'\\n')\n"
        "print('DATASET_ROWS\\t%d' % len(want))\n"
    )
    return "\n".join(
        [
            "set -u",
            f"mkdir -p {_q(sdir)}",
            f"python3 -c {_q(body)} {_q(source)} {_q(ids)} {_q(sdir + '/dataset.jsonl')}",
        ]
    )


def score_guard_script(host: HostConfig, spec: BatchSpec) -> str:
    """Facts a scoring session needs before it may spend anything. Writes nothing.

    Prints tab-separated KEY\\tVALUE lines and nothing else.
    """
    preds = preds_path(host, spec)
    sdir = score_dir(host, spec)
    venv = scoring_python(host)
    return "\n".join(
        [
            "set -u",
            f"P={_q(preds)}",
            f"D={_q(sdir)}",
            f"V={_q(venv)}",
            'if [ -f "$P" ]; then printf "PREDS\\tpresent\\n"; else printf "PREDS\\tabsent\\n"; fi',
            'if [ -f "$P" ]; then printf "PREDS_ROWS\\t%s\\n" "$(grep -c . "$P")"; else printf "PREDS_ROWS\\t0\\n"; fi',
            # A row with an empty model_patch scores as "not resolved" and is
            # indistinguishable in the report from a patch that was tried and
            # failed. Counted here, where the difference is still visible, as
            # rows minus the rows whose patch is the empty string.
            'if [ -f "$P" ]; then',
            '  e=$(grep -c \'"model_patch": *""\' "$P" || true)',
            '  printf "PREDS_EMPTY\\t%s\\n" "$e"',
            'else printf "PREDS_EMPTY\\t0\\n"; fi',
            'if [ -x "$V" ]; then',
            '  v=$("$V" -c "import swebench;print(swebench.__version__)" 2>/dev/null)',
            '  printf "SWEBENCH\\t%s\\n" "${v:-unimportable}"',
            'else printf "SWEBENCH\\tabsent\\n"; fi',
            'if [ -d "$D" ]; then printf "SCORE_DIR\\tpresent\\n"; else printf "SCORE_DIR\\tabsent\\n"; fi',
            # RUNNING is read before the decoy is planted, so the decoy can
            # never be reported as a live scoring run. Then the decoy goes in
            # and the same pattern must find it: a pattern that has stopped
            # matching anything looks exactly like an idle machine.
            f'pgrep -af "{SCORE_PROCESS_PATTERN}" | grep -vF {_q(SCORE_DECOY_MARK)} | while IFS= read -r line; do '
            'printf "RUNNING\\t%s\\n" "$(printf "%s" "$line" | cut -c1-300)"; done',
            f"setsid nohup bash -c 'sleep 6; echo {SCORE_DECOY_MARK}' < /dev/null > /dev/null 2>&1 &",
            "sleep 1",
            f'printf "DECOY_HIT\\t%s\\n" "$(pgrep -af "{SCORE_PROCESS_PATTERN}" | grep -cF {_q(SCORE_DECOY_MARK)})"',
            "df -BG --output=avail " + _q(host.docker_disk) + ' | tail -1 | tr -dc "0-9" '
            '| { read -r a; printf "DISK_FREE_GB\\t%s\\n" "$a"; }',
        ]
    )


def score_script(
    host: HostConfig,
    spec: BatchSpec,
    *,
    run_id: str,
    predictions: str,
    dataset: str,
    max_workers: int,
    timeout: int,
) -> str:
    """One harness invocation, run detached, its log kept beside the reports."""
    sdir = score_dir(host, spec)
    venv = scoring_python(host)
    return "\n".join(
        [
            "set -u",
            f"cd {_q(sdir)}",
            f"mkdir -p {_q(REPORT_DIR)} runlogs",
            f"setsid nohup {_q(venv)} -m swebench.harness.run_evaluation"
            f" --dataset_name {_q(dataset)}"
            " --split test"
            f" --predictions_path {_q(predictions)}"
            f" --max_workers {max_workers}"
            f" --timeout {timeout}"
            f" --run_id {_q(run_id)}"
            f" --report_dir {_q(REPORT_DIR)}"
            f" < /dev/null >> runlogs/{_q(run_id)}.log 2>&1 &",
            "sleep 5",
            f'pgrep -af {_q(SCORE_PROCESS_PATTERN)} | grep -F -- {_q("--run_id " + run_id)} '
            "| head -1 | cut -c1-160 || true",
        ]
    )


def read_report(text: str) -> dict[str, Any]:
    """The harness's report json, reduced to the counts a cell needs.

    ``resolved_ids`` is the list the paper's numbers come from; the rest is
    kept so that a run whose ids and whose counts disagree is visible rather
    than averaged away.
    """
    data = json.loads(text)
    resolved = list(data.get("resolved_ids", []))
    return {
        "total": int(data.get("total_instances", 0)),
        "submitted": int(data.get("submitted_instances", 0)),
        "completed": int(data.get("completed_instances", 0)),
        "resolved": len(resolved),
        "resolved_ids": resolved,
        "unresolved_ids": list(data.get("unresolved_ids", [])),
        "error_ids": list(data.get("error_ids", [])),
        "empty_patch_ids": list(data.get("empty_patch_ids", [])),
    }


def gold_verdict(report: dict[str, Any]) -> tuple[bool, str]:
    """Whether a gold run says the environment can judge anything at all."""
    total = report["total"]
    resolved = report["resolved"]
    if total == 0:
        return False, "gold run scored no instances"
    if resolved == total:
        return True, f"gold resolves {resolved}/{total}"
    unresolved = report["unresolved_ids"] + report["error_ids"]
    return False, (
        f"gold resolves only {resolved}/{total}; the environment cannot judge "
        f"{len(unresolved)} tasks, so no rate from this session is readable: "
        f"{unresolved[:5]}"
    )
