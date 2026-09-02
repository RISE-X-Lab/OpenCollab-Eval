"""Write the frozen task suite, its replication subset, and their manifest.

Run once. The four files it writes are pre-registration artifacts: the grid
cites them, so re-running with a different seed or a different frame is a new
pre-registration and not an update. The manifest records the seed, the frame
digest, the per-stratum counts, every skipped instance with its reason, and the
digest of the code that produced them, because a suite whose provenance is
"someone ran a script" cannot be audited later.

The replication subset is written twice on purpose. ``subset-50.csv`` is the
whole ordered draw and ``subset-30.csv`` is its first 30 rows, which are the
ones run first. Writing the short file as a prefix rather than as its own draw
is what lets the subset be grown later without any run already paid for
becoming unusable: extension appends rows, and no row that was run moves.

The pre-flight check is deliberately not run from here: whether an image exists
is a fact about one machine at one moment, so the caller passes the list of
image references that machine has (``docker images --format
'{{.Repository}}:{{.Tag}}'``) and the manifest records which machine it came
from. The list is required rather than optional, because a suite drawn without
one would silently be a suite nobody checked.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from opencollab_eval.experiment.task_sampling import (
    FrameRow,
    draw_ordered_list,
    stratum_counts,
)

_LIBRARY = Path(task_sampling_source := __file__).resolve().parent.parent / "experiment" / "task_sampling.py"

IMAGE_PREFIX = "swebench/sweb.eval.x86_64."


def image_reference(instance_id: str) -> str:
    """The published container image for one SWE-bench instance."""
    return f"{IMAGE_PREFIX}{instance_id.replace('__', '_1776_')}:latest"


def read_frame(path: Path) -> list[FrameRow]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [
            FrameRow(row["instance_id"].strip(), row["repo"].strip(), row["difficulty"].strip())
            for row in csv.DictReader(handle)
        ]
    identifiers = {row.instance_id for row in rows}
    if len(identifiers) != len(rows):
        raise SystemExit("the frame repeats an instance_id")
    return sorted(rows, key=lambda row: row.instance_id)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame", type=Path, required=True, help="CSV of instance_id,repo,difficulty")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--suite-size", type=int, default=100)
    parser.add_argument("--draw-size", type=int, default=110)
    parser.add_argument("--subset-size", type=int, default=50, help="the whole ordered replication subset")
    parser.add_argument("--subset-head-size", type=int, default=30, help="the prefix of it that runs first")
    parser.add_argument("--cap", type=float, default=0.30)
    parser.add_argument("--images", type=Path, required=True, help="file of image references the run host has")
    parser.add_argument("--images-host", default="", help="which host --images was read from")
    parser.add_argument("--seed-status", default="frozen", help="recorded verbatim in the manifest")
    arguments = parser.parse_args(argv)

    frame = read_frame(arguments.frame)
    by_id = {row.instance_id: row for row in frame}
    available = {line.strip() for line in arguments.images.read_text(encoding="utf-8").splitlines() if line.strip()}

    draw = draw_ordered_list(
        frame,
        seed=arguments.seed,
        head_size=arguments.suite_size,
        total_size=arguments.draw_size,
        cap=arguments.cap,
    )
    suite: list[str] = []
    skipped: list[dict[str, str]] = []
    for instance_id in draw.ordered:
        if len(suite) >= arguments.suite_size:
            break
        reference = image_reference(instance_id)
        if reference not in available:
            skipped.append({"instance_id": instance_id, "image": reference, "reason": "image absent on run host"})
            continue
        suite.append(instance_id)
    if len(suite) < arguments.suite_size:
        raise SystemExit(f"only {len(suite)} of {arguments.suite_size} instances passed pre-flight")

    subset = draw_ordered_list(
        [by_id[instance_id] for instance_id in suite],
        seed=arguments.seed,
        head_size=arguments.subset_head_size,
        total_size=arguments.subset_size,
        cap=arguments.cap,
        namespace="subset",
    )
    subset_head = subset.ordered[: arguments.subset_head_size]

    arguments.out_dir.mkdir(parents=True, exist_ok=True)
    def table(instance_ids: Sequence[str]) -> list[dict[str, str]]:
        return [
            {
                "order": str(position),
                "instance_id": instance_id,
                "repo": by_id[instance_id].repo,
                "difficulty": by_id[instance_id].difficulty,
                "image": image_reference(instance_id),
            }
            for position, instance_id in enumerate(instance_ids, 1)
        ]

    columns = ["order", "instance_id", "repo", "difficulty", "image"]
    write_csv(arguments.out_dir / f"suite-{arguments.suite_size}.csv", table(suite), columns)
    write_csv(arguments.out_dir / f"subset-{arguments.subset_size}.csv", table(subset.ordered), columns)
    write_csv(arguments.out_dir / f"subset-{arguments.subset_head_size}.csv", table(subset_head), columns)

    frame_strata = stratum_counts(frame)
    suite_strata = stratum_counts([by_id[instance_id] for instance_id in suite])
    subset_strata = stratum_counts([by_id[instance_id] for instance_id in subset.ordered])
    head_strata = stratum_counts([by_id[instance_id] for instance_id in subset_head])
    manifest = {
        "seed": arguments.seed,
        "seed_status": arguments.seed_status,
        "cap": arguments.cap,
        "sizes": {
            "frame": len(frame),
            "draw": arguments.draw_size,
            "suite": len(suite),
            "subset": len(subset.ordered),
            "subset_head": len(subset_head),
        },
        "subset_note": (
            f"subset-{arguments.subset_size}.csv is the whole ordered replication subset; "
            f"its first {arguments.subset_head_size} rows are subset-{arguments.subset_head_size}.csv, "
            "the ones run first. Extending the subset appends to this order and moves no row."
        ),
        "frame": {
            "name": "SWE-bench Verified",
            "path": arguments.frame.name,
            "sha256": digest(arguments.frame),
        },
        "script_sha256": digest(Path(__file__).resolve()),
        "library_sha256": digest(_LIBRARY),
        "preflight": {
            "images_host": arguments.images_host,
            "images_seen": len(available),
            "skipped": skipped,
        },
        "repository_counts": {
            repo: {
                "frame": count,
                "suite": Counter(by_id[i].repo for i in suite).get(repo, 0),
                "subset": Counter(by_id[i].repo for i in subset.ordered).get(repo, 0),
                "subset_head": Counter(by_id[i].repo for i in subset_head).get(repo, 0),
            }
            for repo, count in sorted(Counter(row.repo for row in frame).items())
        },
        "stratum_counts": [
            {
                "repo": repo,
                "difficulty": difficulty,
                "frame": count,
                "suite": suite_strata.get((repo, difficulty), 0),
                "subset": subset_strata.get((repo, difficulty), 0),
                "subset_head": head_strata.get((repo, difficulty), 0),
            }
            for (repo, difficulty), count in sorted(frame_strata.items())
        ],
        "ordered_draw": list(draw.ordered),
    }
    (arguments.out_dir / "sampling-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"suite={len(suite)} subset={len(subset.ordered)} head={len(subset_head)} skipped={len(skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
