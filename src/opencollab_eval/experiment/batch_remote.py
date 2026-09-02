"""What the launcher asks the host, and how it reads the answers.

The host side is plain bash so that it runs under ``ssh <host> 'bash -s'`` with
the script on stdin: nothing is copied to the machine, and the checks run
against the checkout that will run the batch, not against a local copy of it.
Every script prints tab-separated ``KEY\\tVALUE`` lines and nothing else, so the
local side parses facts rather than prose.

The pre-flight is the written form of the project's rule that a paid batch is
launched only after three questions are answered: did the thing you meant to
vary actually change (pins, clean trees, card bytes, import path), is what you
mean to record going to be recorded (the three switches, the model file), and
can the machine run it (images, disk, no other launch into the same out-dir).
One check checks the checker: it plants a decoy process and requires the
running-batch pattern to see it, because a pattern that matches nothing looks
identical to a machine with nothing running.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Any

from opencollab_eval.experiment.batch_spec import (
    BATCH_PROCESS_PATTERN,
    BatchSpec,
    HostConfig,
    spec_digest,
)

# The decoy's command line must contain the whole pattern string, unbracketed,
# or the control tests nothing. Pinned in tests.
DECOY_MARK = "opencollab_eval.generation.gen_prediction_batch decoy-for-preflight"


def _q(value: str) -> str:
    return shlex.quote(value)


def _disk_line(host: HostConfig) -> str:
    return (
        f'printf "DISK_FREE_GB\\t%s\\n" '
        f'"$(df -BG --output=avail {_q(host.docker_disk)} 2>/dev/null | tail -1 | tr -dc 0-9)"'
    )


def preflight_script(
    spec: BatchSpec,
    host: HostConfig,
    card_files: list[str],
    images: list[str],
) -> str:
    """Read-only facts about the host, one ``KEY\\tVALUE`` line each."""
    workdir = host.workdir
    oc = f"{workdir}/{host.opencollab_dir}"
    ev = f"{workdir}/{host.eval_dir}"
    team_yaml = f"{oc}/configs/team.handoff.{spec.cell}.yaml" if spec.cell else ""
    model_env = f"{oc}/{spec.model_env}"
    lines = [
        "set -u",
        f"W={_q(workdir)}; OC={_q(oc)}; EV={_q(ev)}; PY={_q(host.python)}; PP={_q(host.pythonpath)}",
        'printf "OC_HEAD\\t%s\\n" "$(git -C "$OC" rev-parse HEAD 2>/dev/null || echo none)"',
        'printf "OC_DIRTY\\t%s\\n" "$(git -C "$OC" status --porcelain 2>/dev/null | wc -l)"',
        'printf "EVAL_HEAD\\t%s\\n" "$(git -C "$EV" rev-parse HEAD 2>/dev/null || echo none)"',
        'printf "EVAL_DIRTY\\t%s\\n" "$(git -C "$EV" status --porcelain 2>/dev/null | wc -l)"',
        # The venv's own opencollab is an installed copy of another checkout;
        # only PYTHONPATH puts the pinned one first. Ask python, not the shell.
        'IMPORTS="$(cd "$W" && PYTHONPATH="$PP" "$PY" -c '
        "'import opencollab, opencollab_eval; print(opencollab.__file__); print(opencollab_eval.__file__)' "
        '2>&1 || true)"',
        'printf "IMPORT_OC\\t%s\\n" "$(printf "%s\\n" "$IMPORTS" | sed -n 1p)"',
        'printf "IMPORT_EVAL\\t%s\\n" "$(printf "%s\\n" "$IMPORTS" | sed -n 2p)"',
    ]
    for rel in card_files:
        lines.append(
            f'if [ -f "$OC"/{_q(rel)} ]; then printf "CARD\\t%s\\t%s\\n" {_q(rel)} '
            f'"$(sha256sum "$OC"/{_q(rel)} | cut -d" " -f1)"; else printf "CARD\\t%s\\tmissing\\n" {_q(rel)}; fi'
        )
    if team_yaml:
        snippet = (
            "import json; from opencollab.teams import declared_role_prompt_digests as d; "
            f"print(json.dumps(d({team_yaml!r}), sort_keys=True))"
        )
        lines.append(f'printf "DIGESTS\\t%s\\n" "$(cd "$W" && PYTHONPATH="$PP" "$PY" -c {_q(snippet)} 2>&1 | tail -1)"')
    lines += [
        f"ME={_q(model_env)}",
        'if [ -f "$ME" ]; then printf "MODEL_ENV\\tpresent\\n"; '
        'printf "MODEL\\t%s\\n" "$(grep -E "^OPENCOLLAB_MODEL=" "$ME" | tail -1 | cut -d= -f2-)"; '
        'printf "PROVIDER\\t%s\\n" "$(grep -E "^OPENCOLLAB_PROVIDER=" "$ME" | tail -1 | cut -d= -f2-)"; '
        'printf "BASE_URL_SHA\\t%s\\n" "$(grep -E "^OPENCOLLAB_BASE_URL=" "$ME" | tail -1 | cut -d= -f2- '
        '| tr -d "\\n" | sha256sum | cut -d" " -f1)"; '
        'else printf "MODEL_ENV\\tabsent\\n"; fi',
        _disk_line(host),
        'IMG="$(mktemp)"; docker images --format "{{.Repository}}:{{.Tag}}" > "$IMG" 2>/dev/null || true',
    ]
    for image in sorted(set(images)):
        lines.append(f'grep -qxF -- {_q(image)} "$IMG" || printf "IMAGE_MISSING\\t%s\\n" {_q(image)}')
    lines += [
        'rm -f "$IMG"',
        f'pgrep -af "{BATCH_PROCESS_PATTERN}" | grep -vF {_q(DECOY_MARK)} | while IFS= read -r line; do '
        'printf "RUNNING\\t%s\\n" "$(printf "%s" "$line" | cut -c1-300)"; done',
        # Positive control: two commands, so bash keeps its argv instead of exec-ing.
        f"setsid nohup bash -c 'sleep 6; echo {DECOY_MARK}' < /dev/null > /dev/null 2>&1 &",
        "sleep 1",
        f'printf "DECOY_HIT\\t%s\\n" "$(pgrep -af "{BATCH_PROCESS_PATTERN}" | grep -cF {_q(DECOY_MARK)})"',
        f'if [ -d "$W"/{_q(spec.name)} ]; then if [ -f "$W"/{_q(spec.name)}/batch.json ]; then '
        f'printf "OUTDIR\\t%s\\n" "$("$PY" -c '
        "\"import json,sys; print(json.load(open(sys.argv[1])).get('spec_digest',''))\" "
        f'"$W"/{_q(spec.name)}/batch.json 2>/dev/null || echo unreadable)"; '
        'else printf "OUTDIR\\tno-batchjson\\n"; fi; else printf "OUTDIR\\tabsent\\n"; fi',
        f'if [ -f "$W"/{_q(spec.instances_file)} ]; then printf "REMOTE_INSTANCES_SHA\\t%s\\n" '
        f'"$(sha256sum "$W"/{_q(spec.instances_file)} | cut -d" " -f1)"; '
        'else printf "REMOTE_INSTANCES_SHA\\tabsent\\n"; fi',
        "",
    ]
    return "\n".join(lines)


def status_script(spec: BatchSpec, host: HostConfig) -> str:
    workdir = host.workdir
    out = f"{workdir}/{spec.name}"
    preds = f"preds-{spec.arm}.jsonl"
    count_metrics = (
        "import json,sys,collections\n"
        "c=collections.Counter()\n"
        "for line in open(sys.argv[1], encoding='utf-8'):\n"
        "    line=line.strip()\n"
        "    if not line: continue\n"
        "    r=json.loads(line); c[str(((r.get('run_summary') or {}).get('status')))]+=1\n"
        "for k,v in sorted(c.items()): print('STATUS\\t%s\\t%d'%(k,v))\n"
    )
    own = _q("--out-dir " + spec.name + " ")
    return "\n".join(
        [
            "set -u",
            f"W={_q(workdir)}; O={_q(out)}; PY={_q(host.python)}",
            f'printf "ALIVE\\t%s\\n" "$(pgrep -af "{BATCH_PROCESS_PATTERN}" | grep -cF -- {own})"',
            f'printf "TOTAL\\t%s\\n" "$(wc -l < "$W"/{_q(spec.instances_file)} 2>/dev/null || echo 0)"',
            f"for f in manifest.jsonl {_q(preds)} metrics.jsonl; do "
            'printf "LINES\\t%s\\t%s\\n" "$f" "$(wc -l < "$O/$f" 2>/dev/null || echo 0)"; done',
            f'if [ -f "$O/metrics.jsonl" ]; then "$PY" -c {_q(count_metrics)} "$O/metrics.jsonl" '
            "2>/dev/null || true; fi",
            _disk_line(host),
            f'if [ -f "$W"/{_q(spec.log_file)} ]; then tail -n 3 "$W"/{_q(spec.log_file)} | cut -c1-200 '
            '| while IFS= read -r line; do printf "LOGTAIL\\t%s\\n" "$line"; done; fi',
            "",
        ]
    )


def wait_script(spec: BatchSpec, host: HostConfig, poll_seconds: int) -> str:
    """Block on the host until no driver for this out-dir is alive, then report."""
    guard = f'pgrep -af "{BATCH_PROCESS_PATTERN}" | grep -qF -- {_q("--out-dir " + spec.name + " ")}'
    return "\n".join(
        [
            f"while {guard}; do sleep {int(poll_seconds)}; done",
            'printf "DONE\\t%s\\n" "$(date -Is)"',
            status_script(spec, host),
        ]
    )


def parse_facts(text: str) -> list[tuple[str, ...]]:
    """Every ``KEY\\tVALUE...`` line, in order; anything else is dropped."""
    facts: list[tuple[str, ...]] = []
    for raw in text.splitlines():
        if "\t" not in raw:
            continue
        parts = tuple(p.strip() for p in raw.rstrip("\n").split("\t"))
        if parts[0]:
            facts.append(parts)
    return facts


def fact(facts: list[tuple[str, ...]], key: str, default: str = "") -> str:
    for parts in facts:
        if parts[0] == key and len(parts) > 1:
            return parts[1]
    return default


def facts_all(facts: list[tuple[str, ...]], key: str) -> list[tuple[str, ...]]:
    return [parts[1:] for parts in facts if parts[0] == key]


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    warn: bool = False


def evaluate_preflight(
    spec: BatchSpec,
    host: HostConfig,
    facts: list[tuple[str, ...]],
    expected_cards: dict[str, str],
    instances_sha: str,
) -> list[Check]:
    """Turn host facts into pass/fail lines. A failure means: do not launch."""
    checks: list[Check] = []

    oc_head = fact(facts, "OC_HEAD")
    ev_head = fact(facts, "EVAL_HEAD")
    checks.append(
        Check(
            "pin opencollab",
            oc_head == spec.pins["opencollab"],
            f"host {oc_head[:12]} vs spec {spec.pins['opencollab'][:12]}",
        )
    )
    checks.append(
        Check(
            "pin opencollab_eval",
            ev_head == spec.pins["opencollab_eval"],
            f"host {ev_head[:12]} vs spec {spec.pins['opencollab_eval'][:12]}",
        )
    )
    checks.append(
        Check("clean opencollab", fact(facts, "OC_DIRTY") == "0", f"{fact(facts, 'OC_DIRTY') or '?'} modified paths")
    )
    checks.append(
        Check(
            "clean opencollab_eval",
            fact(facts, "EVAL_DIRTY") == "0",
            f"{fact(facts, 'EVAL_DIRTY') or '?'} modified paths",
        )
    )

    oc_prefix = f"{host.workdir}/{host.opencollab_dir}/"
    ev_prefix = f"{host.workdir}/{host.eval_dir}/"
    imp_oc = fact(facts, "IMPORT_OC")
    imp_ev = fact(facts, "IMPORT_EVAL")
    checks.append(
        Check("import opencollab from pinned checkout", imp_oc.startswith(oc_prefix), imp_oc or "(no answer)")
    )
    checks.append(
        Check("import opencollab_eval from pinned checkout", imp_ev.startswith(ev_prefix), imp_ev or "(no answer)")
    )

    host_cards = {parts[0]: parts[1] for parts in facts_all(facts, "CARD") if len(parts) >= 2}
    for rel, expected in expected_cards.items():
        got = host_cards.get(rel, "absent")
        checks.append(Check(f"card bytes {rel}", got == expected, f"host {got[:12]} vs pinned {expected[:12]}"))

    if spec.cell is not None:
        digests_raw = fact(facts, "DIGESTS")
        try:
            digests = json.loads(digests_raw)
            ok = isinstance(digests, dict) and "analyst" in digests
        except ValueError:
            ok = False
        checks.append(
            Check("role prompt digests computed on host", ok, digests_raw[:160] if not ok else f"{len(digests)} roles")
        )

    checks.append(
        Check(
            "model env file present", fact(facts, "MODEL_ENV") == "present", f"{host.opencollab_dir}/{spec.model_env}"
        )
    )
    model = fact(facts, "MODEL")
    checks.append(Check("model named", bool(model), model or "(OPENCOLLAB_MODEL empty)"))

    free = fact(facts, "DISK_FREE_GB")
    try:
        free_gb = float(free)
    except ValueError:
        free_gb = -1.0
    checks.append(
        Check(
            "docker disk free",
            free_gb >= host.min_free_gb,
            f"{free or '?'} GB free on {host.docker_disk}, need {host.min_free_gb:g}",
        )
    )

    missing_images = [parts[0] for parts in facts_all(facts, "IMAGE_MISSING") if parts]
    checks.append(
        Check(
            "task images present",
            not missing_images,
            f"{len(missing_images)} missing" + (f": {missing_images[:3]}" if missing_images else ""),
        )
    )

    decoy = fact(facts, "DECOY_HIT", "0")
    checks.append(
        Check("running-batch check sees a planted process", decoy not in ("", "0"), f"decoy hits {decoy or '0'}")
    )

    running = [parts[0] for parts in facts_all(facts, "RUNNING") if parts]
    same = [line for line in running if f"--out-dir {spec.name} " in line + " "]
    others = [line for line in running if line not in same]
    checks.append(Check("no driver already writing this out-dir", not same, same[0][:120] if same else "none"))
    if others:
        checks.append(
            Check(
                "other batches running on the host",
                True,
                f"{len(others)} other driver(s); the machine is shared",
                warn=True,
            )
        )

    outdir = fact(facts, "OUTDIR", "absent")
    if outdir == "absent":
        checks.append(Check("out-dir", True, "absent; a fresh batch"))
    elif outdir == spec_digest(spec):
        checks.append(Check("out-dir", True, "exists with this spec's batch.json; the launch resumes it"))
    elif outdir == "no-batchjson":
        checks.append(Check("out-dir", False, "exists without batch.json (a hand-launched batch); pick another name"))
    else:
        checks.append(Check("out-dir", False, f"exists with a different spec ({outdir[:12]}); pick another name"))

    remote_sha = fact(facts, "REMOTE_INSTANCES_SHA", "absent")
    checks.append(
        Check(
            "instance file on host",
            remote_sha in ("absent", instances_sha),
            "absent (will be copied)"
            if remote_sha == "absent"
            else f"host {remote_sha[:12]} vs local {instances_sha[:12]}",
        )
    )
    return checks


def facts_to_record(facts: list[tuple[str, ...]]) -> dict[str, Any]:
    """The host facts worth keeping in batch.json (never the model file's secrets)."""
    digests_raw = fact(facts, "DIGESTS")
    try:
        digests = json.loads(digests_raw) if digests_raw else None
    except ValueError:
        digests = None
    return {
        "opencollab_head": fact(facts, "OC_HEAD"),
        "opencollab_eval_head": fact(facts, "EVAL_HEAD"),
        "import_opencollab": fact(facts, "IMPORT_OC"),
        "import_opencollab_eval": fact(facts, "IMPORT_EVAL"),
        "card_sha256": {parts[0]: parts[1] for parts in facts_all(facts, "CARD") if len(parts) >= 2},
        "role_prompt_sha256": digests,
        "model": fact(facts, "MODEL"),
        "provider": fact(facts, "PROVIDER"),
        "base_url_sha256": fact(facts, "BASE_URL_SHA"),
        "disk_free_gb": fact(facts, "DISK_FREE_GB"),
    }
