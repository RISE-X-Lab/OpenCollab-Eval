"""Order the whole sampling frame for the arms that run it whole.

Run once. ``frame-ordered.csv`` is a pre-registration artifact like the suite:
a frame-wide batch cites it through its ``suite`` field and a slice of its
``order`` column, so re-running with another seed or another frame is a new
pre-registration. Nothing is drawn; the frame is run whole, and the order only
decides which tasks an arm reaches first, so that an arm stopped early is a
random subsample of the frame rather than the alphabetical head of it.

The image pre-flight is the same as the suite's: the caller passes the image
references the run host has, and every instance whose image is absent is
written to the manifest with its reason and left out of the order. The
manifest records the seed, the frame digest, the per-repository counts, and
the digests of the code that produced the file.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from opencollab_eval.commands.draw_task_suite import (
    _LIBRARY,
    digest,
    image_reference,
    read_frame,
    write_csv,
)
from opencollab_eval.experiment.task_sampling import order_frame


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame", type=Path, required=True, help="CSV of instance_id,repo,difficulty")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--name", default="frame-ordered", help="basename of the CSV and of <name>-manifest.json")
    parser.add_argument("--images", type=Path, required=True, help="file of image references the run host has")
    parser.add_argument("--images-host", default="", help="which host --images was read from")
    parser.add_argument("--seed-status", default="frozen", help="recorded verbatim in the manifest")
    arguments = parser.parse_args(argv)

    frame = read_frame(arguments.frame)
    by_id = {row.instance_id: row for row in frame}
    available = {line.strip() for line in arguments.images.read_text(encoding="utf-8").splitlines() if line.strip()}

    ordered: list[str] = []
    skipped: list[dict[str, str]] = []
    for instance_id in order_frame(frame, seed=arguments.seed):
        reference = image_reference(instance_id)
        if reference not in available:
            skipped.append({"instance_id": instance_id, "image": reference, "reason": "image absent on run host"})
            continue
        ordered.append(instance_id)

    table = [
        {
            "order": str(position),
            "instance_id": instance_id,
            "repo": by_id[instance_id].repo,
            "difficulty": by_id[instance_id].difficulty,
            "image": image_reference(instance_id),
        }
        for position, instance_id in enumerate(ordered, 1)
    ]
    arguments.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(arguments.out_dir / f"{arguments.name}.csv", table, ["order", "instance_id", "repo", "difficulty", "image"])

    frame_counts = Counter(row.repo for row in frame)
    ordered_counts = Counter(by_id[instance_id].repo for instance_id in ordered)
    manifest = {
        "seed": arguments.seed,
        "seed_status": arguments.seed_status,
        "namespace": "frame",
        "sizes": {"frame": len(frame), "ordered": len(ordered), "skipped": len(skipped)},
        "frame": {"name": "SWE-bench Verified", "path": arguments.frame.name, "sha256": digest(arguments.frame)},
        "script_sha256": digest(Path(__file__).resolve()),
        "library_sha256": digest(_LIBRARY),
        "preflight": {"images_host": arguments.images_host, "images_seen": len(available), "skipped": skipped},
        "repository_counts": {
            repo: {"frame": frame_counts[repo], "ordered": ordered_counts.get(repo, 0)} for repo in sorted(frame_counts)
        },
        "note": (
            f"{arguments.name}.csv is the whole frame in one seeded order, no cap and no strata; "
            "a frame-wide batch cites it through its suite field and a slice of the order column, "
            "and a prefix of it is a random subsample of the frame."
        ),
    }
    (arguments.out_dir / f"{arguments.name}-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
