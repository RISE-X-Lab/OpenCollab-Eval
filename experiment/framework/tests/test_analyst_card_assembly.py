"""Check Analyst card assembly and isolation of declared treatment changes.

Team files load checked-in cards, so each must equal its registry rendering.
Legacy cards retain the text used by historical batches.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
from opencollab.bootstrap.team_config import load_team_config

from experiment.framework.scripts.analyst_cards import (
    BLOCK_SLOT,
    CAPABILITIES_SLOT,
    CLOSING_LINE,
    load_registry,
    render,
    shared_body,
)
from experiment.framework.tests.paths import FRAMEWORK_ROOT as REPO_ROOT

CONFIGS = REPO_ROOT / "configs"
PROMPTS = CONFIGS / "handoff-experiment"
ROLES = ("analyst", "coder", "tester")

VARIANTS, LADDERS = load_registry()
NAMES = sorted(VARIANTS)

#: Historical batches depend on these legacy cards retaining their original text.
LEGACY_TEAM_FILES = {
    "primary": "team.handoff.primary-legacy.yaml",
    "weak": "team.handoff.weak.yaml",
    "strong": "team.handoff.strong.yaml",
    "default": "team.handoff.default.yaml",
    "norm": "team.handoff.norm.yaml",
    "salience": "team.handoff.salience.yaml",
    "instructed": "team.handoff.instructed.yaml",
    "pool-disclosed": "team.handoff.pool-disclosed.yaml",
}


def _block(name: str) -> str:
    return (PROMPTS / "blocks" / f"{name}.md").read_text(encoding="utf-8")


def _capabilities(name: str) -> str:
    return (PROMPTS / "capabilities" / f"{name}.md").read_text(encoding="utf-8")


def _card_on_disk(name: str) -> str:
    return VARIANTS[name].card_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", NAMES)
def test_every_checked_in_card_is_exactly_what_its_declaration_assembles_to(name: str) -> None:
    """Team files load the checked-in card, so assembly drift changes the treatment."""
    assert _card_on_disk(name) == render(VARIANTS[name])


@pytest.mark.parametrize("name", NAMES)
def test_a_card_is_the_shared_body_everywhere_outside_its_block(name: str) -> None:
    """Removing the treatment block must recover the declared shared body."""
    variant = VARIANTS[name]
    card = _card_on_disk(name)
    block = _block(variant.block)
    assert card.count(block) == 1, name
    assert card.replace(block, BLOCK_SLOT) == shared_body().replace(
        CAPABILITIES_SLOT, _capabilities(variant.capabilities)
    )


def test_cells_that_share_a_bundle_are_identical_outside_their_blocks() -> None:
    """The starved cell's changed tool bundle also changes its capabilities paragraph."""
    reduced: dict[str, set[str]] = {}
    for name in NAMES:
        variant = VARIANTS[name]
        card = _card_on_disk(name)
        reduced.setdefault(variant.capabilities, set()).add(card.replace(_block(variant.block), BLOCK_SLOT))
    for capabilities, texts in reduced.items():
        assert len(texts) == 1, f"cells on the {capabilities!r} bundle disagree outside their blocks"


@pytest.mark.parametrize("name", NAMES)
def test_every_card_ends_on_the_shared_evidence_rule(name: str) -> None:
    """What counts as a verified change is outcome-side; no cell may vary it."""
    assert _card_on_disk(name).endswith("\n" + CLOSING_LINE)
    assert CLOSING_LINE not in _block(VARIANTS[name].block), "the closing line belongs to the shared body"


def test_no_two_cells_assemble_to_the_same_card() -> None:
    """Duplicate cards would present run-to-run noise as a treatment effect."""
    cards = {name: _card_on_disk(name) for name in NAMES}
    duplicates = [
        (a, b) for a, b in itertools.combinations(NAMES, 2) if cards[a] == cards[b]
    ]
    assert not duplicates, duplicates


def test_no_two_block_files_hold_the_same_text() -> None:
    """The registry may point two cells at one block; two blocks may not be one text.

    ``facts-v2`` and ``starved`` deliberately share ``blocks/judge.md`` -- they
    vary capability, not stance. That is one block named once. Two *files* with
    the same content is the drift this catches.
    """
    blocks = sorted({VARIANTS[name].block for name in NAMES})
    texts = {name: _block(name) for name in blocks}
    duplicates = [
        (a, b) for a, b in itertools.combinations(blocks, 2) if texts[a] == texts[b]
    ]
    assert not duplicates, duplicates


def test_every_registered_block_and_bundle_file_exists_and_is_used() -> None:
    """An orphan block is a cell someone forgot to register, or a dead file."""
    used_blocks = {VARIANTS[name].block for name in NAMES}
    on_disk = {p.stem for p in (PROMPTS / "blocks").glob("*.md")}
    assert used_blocks == on_disk
    used_capabilities = {VARIANTS[name].capabilities for name in NAMES}
    assert used_capabilities == {p.stem for p in (PROMPTS / "capabilities").glob("*.md")}


#: Each sentence in a ``capabilities/`` file that claims the Analyst holds a
#: given tool, and the sentence that claims it holds none of the edit tools.
#: The card is the model's only account of its own bundle, so a claim that does
#: not match the bundle is a false premise the model then reasons from.
OWNERSHIP_CLAIMS = {
    "apply_patch": "`apply_patch` and `file_write` edit",
    "file_write": "`apply_patch` and `file_write` edit",
    "run_tests": "`run_tests` runs the test suite",
}
NO_WRITE_DISCLAIMER = "No tool in your bundle edits a file or runs the test suite."


@pytest.mark.parametrize("name", NAMES)
def test_the_card_states_the_bundle_the_team_file_actually_grants(name: str) -> None:
    """The wording and the capability are one declaration or the cell is broken.

    The fourth arm shipped a card that still claimed tools its team file had
    taken away. Nothing errored: the Analyst simply reasoned from a bundle it
    did not have, and the workaround it invented became an artefact of the
    contradiction rather than a finding. So the registry declares the bundle,
    the team file grants it, and the ``capabilities/`` text describes it -- and
    all three have to agree.
    """
    variant = VARIANTS[name]
    granted = tuple(load_team_config(path=str(CONFIGS / variant.team_file)).roles["analyst"].tools)
    assert variant.tools == granted, (
        f"{name}: variants.yaml declares {variant.tools} but {variant.team_file} grants {granted}"
    )

    stated = _capabilities(variant.capabilities)
    for tool, claim in OWNERSHIP_CLAIMS.items():
        assert (claim in stated) == (tool in granted), (
            f"{name}: the card {'claims' if claim in stated else 'omits'} {tool}, "
            f"the bundle {'has' if tool in granted else 'lacks'} it"
        )
    edits = {"apply_patch", "file_write"} & set(granted)
    assert (NO_WRITE_DISCLAIMER in stated) == (not edits), name


@pytest.mark.parametrize("name", NAMES)
def test_each_cell_seats_the_baseline_team(name: str) -> None:
    """Roster, topology, and both teammates are the instrument, not the treatment.

    A cell that quietly dropped ``message_agent`` or a return edge would still
    run and still produce a transcript -- one in which the handoff never
    happened. The zero would read as a fact about the model.
    """
    variant = VARIANTS[name]
    team = load_team_config(path=str(CONFIGS / variant.team_file))
    baseline = load_team_config(path=str(CONFIGS / "team.handoff.experiment.yaml"))
    assert team.entry == baseline.entry
    assert sorted(team.roles) == sorted(baseline.roles)
    for role in ("coder", "tester"):
        assert team.roles[role].tools == baseline.roles[role].tools, role
        assert team.roles[role].prompt == baseline.roles[role].prompt, role
    assert team.roles["analyst"].prompt == _card_on_disk(name)
    walked = {(s, d) for s in ROLES for d in ROLES if team.topology.allows(s, d)}
    assert walked == {(s, d) for s in ROLES for d in ROLES if s != d}


ANALYST_PROMPT_LINE = "    prompt_file: handoff-experiment/"


def _team_file_body(path: Path) -> str:
    """The configuration below the header comment, with the two Analyst lines masked.

    The header comments differ on purpose -- each one records what its cell is
    and what it may not be pooled with -- so they are not part of this
    comparison. Everything the loader reads is.
    """
    text = path.read_text(encoding="utf-8")
    body = text[text.index("entry:"):]
    lines = body.splitlines(keepends=True)
    out, i = [], 0
    while i < len(lines):
        if lines[i].startswith(ANALYST_PROMPT_LINE) and "coder.md" not in lines[i] and "tester.md" not in lines[i]:
            out.append("PROMPT_FILE\n")
            out.append("TOOLS\n")  # the Analyst's bundle, the one other allowed difference
            i += 2
            continue
        out.append(lines[i])
        i += 1
    return "".join(out)


def test_every_team_file_differs_only_in_the_analysts_two_lines() -> None:
    """Declared research variants and the current main entry share one roster.

    A cell is selected by naming its team file, so each one repeats the whole
    configuration. Only the Analyst's ``prompt_file`` and its ``tools`` may
    differ; a drift anywhere else -- a tool added to the Coder, an edge dropped,
    the entry role changed -- would make two cells incomparable while both
    continued to run.
    """
    bodies = {
        path.name: _team_file_body(path)
        for path in sorted(CONFIGS.glob("team.handoff.*.yaml"))
    }
    assert "team.handoff.experiment.yaml" in bodies
    assert len(bodies) == len(VARIANTS) + len(LEGACY_TEAM_FILES) + 1
    distinct = set(bodies.values())
    assert len(distinct) == 1, sorted(
        name for name, body in bodies.items() if body != next(iter(distinct))
    )


def test_the_first_generation_cards_are_still_where_their_batches_left_them() -> None:
    """``legacy/`` is an archive, not a staging area.

    These eight cards carry the attribution of every batch measured before the
    shared body was rewritten. They are not assembled -- porting one of their
    blocks onto ``shared.md`` produces a different cell, because the body around
    it is a later generation -- so none of them may appear in the registry.
    """
    legacy = {p.name for p in (PROMPTS / "legacy").glob("*.md")}
    assert legacy == {f"analyst{suffix}.md" for suffix in
                      ("", ".weak", ".strong", ".default", ".norm", ".salience",
                       ".instructed", ".pool-disclosed")}
    assembled = {VARIANTS[name].card_name for name in NAMES}
    assert not (legacy & assembled)


@pytest.mark.parametrize("level", sorted(LEGACY_TEAM_FILES))
def test_each_first_generation_team_file_still_loads_its_archived_card(level: str) -> None:
    """Moving the cards into ``legacy/`` must not have unhooked any of them."""
    team = load_team_config(path=str(CONFIGS / LEGACY_TEAM_FILES[level]))
    suffix = "" if level == "primary" else f".{level}"
    archived = (PROMPTS / "legacy" / f"analyst{suffix}.md").read_text(encoding="utf-8")
    assert team.roles["analyst"].prompt == archived


@pytest.mark.parametrize("name", NAMES)
def test_every_cell_carries_a_note(name: str) -> None:
    """Pooling two cells produces a number, not an error.

    The note is where the registry says what a cell is, so whoever selects it
    reads it. A ported first-generation block additionally has to say that its
    body is a later generation -- ``variants.yaml`` states that rule in full.
    """
    note = VARIANTS[name].note.strip()
    assert len(note) > 40, name
