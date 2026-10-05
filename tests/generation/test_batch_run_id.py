"""Each manifest row names the run id its generator process was given.

The manifest is written by the driver and the metrics row and trajectory by
the run, so the three join only if the driver chooses the id and hands it on.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys


def test_every_manifest_row_names_the_run_id_its_generator_received(tmp_path):
    generator = tmp_path / "generator.py"
    generator.write_text(
        "import os,sys\nfrom pathlib import Path\n"
        "iid=Path(sys.argv[sys.argv.index('--instance-file')+1]).stem\n"
        f"(Path({str(tmp_path)!r})/(iid+'.run_id')).write_text(os.environ['OPENCOLLAB_RUN_ID'])\n"
    )
    runner = (
        "import sys\nfrom opencollab_eval.generation import gen_prediction_batch as batch\n"
        f"generator={str(generator)!r}\n"
        "def command(**kwargs):\n"
        " return [sys.executable,generator,'--instance-file',str(kwargs['instance_path'])]\n"
        "batch.build_command=command\nraise SystemExit(batch.main(sys.argv[1:]))\n"
    )
    instances = tmp_path / "instances.jsonl"
    instances.write_text("".join(json.dumps({"instance_id": f"a__a-{i}", "repo": "a/a"}) + "\n" for i in range(2)))

    # The fake generator writes no prediction, so the driver exits non-zero
    # reporting the runs as missing; the manifest is written either way.
    subprocess.run(
        [sys.executable, "-c", runner, "--instances", str(instances), "--arm", "single",
         "--out-dir", str(tmp_path / "out"), "--concurrency", "1"],
        capture_output=True, timeout=60,
    )

    rows = [json.loads(line) for line in (tmp_path / "out" / "manifest.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    for row in rows:
        assert re.fullmatch(r"single-[0-9a-f]{32}", row["run_id"])
        assert (tmp_path / f"{row['instance_id']}.run_id").read_text() == row["run_id"]
    assert len({row["run_id"] for row in rows}) == 2
