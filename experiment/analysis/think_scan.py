"""Inspect reasoning and tool use in explicitly supplied research batches."""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
from collections.abc import Sequence

WRITE = {"apply_patch", "file_write"}
TEAM = ("coder", "delegat", "message_agent", "team_status", "hand it over", "hand off", "teammate")
WRITEY = re.compile(r"(>\s*[^&\s]|>>|open\([^)]*['\"][wa]|sed -i|tee |cat\s*<<|dd of=)")


def scan(cells: Sequence[str]) -> None:
    for cell in sorted(cells):
        name = os.path.basename(cell)
        mp = os.path.join(cell, "metrics.jsonl")
        done = set()
        if os.path.exists(mp):
            for line in open(mp, encoding="utf-8"):
                done.add(json.loads(line)["instance_id"])
        for tj in sorted(glob.glob(cell + "/**/team.json", recursive=True)):
            inst = next((p for p in tj.split("/") if "__" in p), "?")
            if inst not in done:
                continue
            d = os.path.dirname(tj)
            calls = collections.Counter()
            asst = {}
            w = collections.Counter()
            rchars = 0
            rhits = collections.Counter()
            bashw = 0
            for af in sorted(glob.glob(os.path.join(d, "agent_*.json"))):
                a = json.load(open(af))
                aid = a.get("aid")
                n = 0
                for m in a.get("messages") or []:
                    if m.get("role") == "assistant":
                        n += 1
                    r = m.get("reasoning_content") or ""
                    if r and aid == 0:
                        rchars += len(r)
                        for k in TEAM:
                            rhits[k] += r.lower().count(k)
                    for tc in m.get("tool_calls") or []:
                        fn = (tc.get("function") or {}).get("name")
                        calls[fn] += 1
                        if fn in WRITE:
                            w[aid] += 1
                        if fn == "bash" and aid == 0:
                            cmd = json.loads((tc.get("function") or {}).get("arguments") or "{}").get("command", "")
                            if WRITEY.search(cmd):
                                bashw += 1
                asst[aid] = n
            seats = "/".join(str(asst.get(i, 0)) for i in (0, 1, 2))
            terms = {key: value for key, value in rhits.items() if value}
            print(
                f"{name:<24s} {inst[:30]:<30s} seats {seats:<12s} "
                f"messages={calls.get('message_agent', 0):<2d} status={calls.get('team_status', 0):<2d} "
                f"writes={str(dict(w)):<10s} bash_writes={bashw:<2d} reasoning={rchars:6d} "
                f"teammate_terms={terms or 'none'}"
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_dirs", nargs="+", help="batch directories containing metrics and team trajectories")
    args = parser.parse_args(argv)
    scan(args.batch_dirs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
