# Framework collaboration experiments

This directory owns the experiment-only team configurations, exact role cards,
assembly source, and instrument checks moved from OpenCollab
`d2736f3a`. The 153 configuration files, including 106 Markdown prompt inputs,
retain their original bytes. They are research inputs, not extra user guides.
OpenCollab keeps its built-in team, reusable `team.collab.yaml`, and general examples.

| Material | Use |
| --- | --- |
| `configs/handoff-experiment/` | Handoff wording and capability conditions; `legacy/` preserves eight first-generation cards |
| `configs/dual-candidate/`, `configs/s2dual/` | Adopter and two Coder conditions, including profile and budget disclosures |
| `configs/s2tools/`, `configs/s2sc/` | Tool and topology interventions |
| `configs/g22-mirror/` | G22 role and evidence protocol mirror |
| `configs/team.*.yaml` | Explicit rosters, tools, prompts and directed edges for each condition |
| `scripts/analyst_cards.py` | Assembly of the 40 generated cards from templates and registries |
| `tests/` | The transferred instrument assertions; no assertion was removed |
| [Collaborating-team study](collaborating-team.md) | August 31 observations, failure mechanisms and recorded smoke run |

## Reproduce a historical batch

Keep the two repository commits named in its batch specification and use the
original OpenCollab checkout. Historical batch launchers still resolve
`configs/team.*.yaml` in that pinned checkout and verify the exact prompt bytes.
The [batch guide](../batches/README.md) describes those checks. Do not repoint an
old batch at this directory or silently fall back to current prompts.

For the last framework snapshot before extraction:

```bash
git -C /path/to/OpenCollab worktree add --detach /tmp/opencollab-research d2736f3a
```

For older results, use the commit actually recorded with that batch; this
snapshot is not a replacement for it. First- and second-generation shared
bodies are distinct treatments even when their closing blocks match.

## Use a configuration from its new location

Install a compatible OpenCollab runtime, then name the full team path explicitly:

```bash
opencollab team show --team-config /path/to/OpenCollab-Eval/experiment/framework/configs/team.s2dual-plain.yaml
opencollab --team-config /path/to/OpenCollab-Eval/experiment/framework/configs/team.s2dual-plain.yaml --workspace /path/to/task
```

For a prebuilt roster, call the public `OpenCollab.team()` API with that path and
`prebuild_team=True`. Record the framework revision, evaluation revision, model,
budget, role profiles, exact prompts and realized behavior for a new run. A
current runtime run is a new condition, not an automatic reproduction of an old one.
The current arm audit reads these owned inputs (or the evaluation checkout named
by `OPENCOLLAB_EVAL_SOURCE_ROOT`). An explicit `OPENCOLLAB_SOURCE_ROOT` selects
the historical layout and does not fall back when its prompt is missing.
The historical batch launcher remains bound to its original checkout layout;
these explicit CLI/SDK paths do not change its saved specifications.

## Maintain and verify the instrument

Use Python 3.10 or newer with the development dependencies of the framework
snapshot. From the evaluation checkout:

```bash
python experiment/framework/scripts/analyst_cards.py
python -m pytest -q -o asyncio_mode=auto experiment/framework/tests
```

Edit the shared templates, blocks, capabilities, topology or registry first,
then run the assembler with `--write` and rerun both checks. Keep the legacy
cards unchanged for attribution. Generated cards are the files loaded by the
team configurations, so checking the templates alone is insufficient.

These transferred checks exercise version-specific framework prompt composition
and configuration internals as well as card identity. They run separately from
the evaluation package's public-API tests, against the recorded compatible
framework source in CI. They do not add internal imports to evaluation production
code or relax its public-API boundary. Framework runtime regressions that formerly
borrowed these cards now use self-contained inline configuration fixtures.
