import json, glob, os, collections, re
home = os.path.expanduser("~/oc-team-smoke")
WRITE = {"apply_patch", "file_write"}
TEAM = ("coder", "delegat", "message_agent", "team_status", "hand it over", "hand off", "teammate")
WRITEY = re.compile(r"(>\s*[^&\s]|>>|open\([^)]*['\"][wa]|sed -i|tee |cat\s*<<|dd of=)")
for cell in sorted(glob.glob(home + "/think-*")):
    name = os.path.basename(cell)
    mp = os.path.join(cell, "metrics.jsonl")
    done = set()
    if os.path.exists(mp):
        for l in open(mp): done.add(json.loads(l)["instance_id"])
    for tj in sorted(glob.glob(cell + "/**/team.json", recursive=True)):
        inst = next((p for p in tj.split("/") if "__" in p), "?")
        if inst not in done: continue
        d = os.path.dirname(tj)
        calls = collections.Counter(); asst = {}; w = collections.Counter()
        rchars = 0; rhits = collections.Counter(); bashw = 0
        for af in sorted(glob.glob(os.path.join(d, "agent_*.json"))):
            a = json.load(open(af)); aid = a.get("aid"); n = 0
            for m in (a.get("messages") or []):
                if m.get("role") == "assistant": n += 1
                r = m.get("reasoning_content") or ""
                if r and aid == 0:
                    rchars += len(r)
                    for k in TEAM: rhits[k] += r.lower().count(k)
                for tc in (m.get("tool_calls") or []):
                    fn = (tc.get("function") or {}).get("name")
                    calls[fn] += 1
                    if fn in WRITE: w[aid] += 1
                    if fn == "bash" and aid == 0:
                        cmd = json.loads((tc.get("function") or {}).get("arguments") or "{}").get("command", "")
                        if WRITEY.search(cmd): bashw += 1
            asst[aid] = n
        seats = "/".join(str(asst.get(i, 0)) for i in (0, 1, 2))
        print("%-24s %-30s 座位 %-12s msg=%-2d ts=%-2d 写:%-10s bash写=%-2d reasoning=%6d 队友词=%s"
              % (name, inst[:30], seats, calls.get("message_agent", 0), calls.get("team_status", 0),
                 str(dict(w)), bashw, rchars, {k: v for k, v in rhits.items() if v} or "无"))
