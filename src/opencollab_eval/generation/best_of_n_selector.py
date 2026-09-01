"""Which of the N candidates is submitted, and why this rule is a placeholder.

The Best-of-N arm runs N non-communicating candidates and submits one of them.
The selector is the whole of "which one": everything else about the arm is the
single-agent arm with the pool split N ways. So the selector is written here,
on its own, as a pure function of what the candidates produced -- and not
inside the generator, where it would be reachable only by starting containers
and where the temptation to look at something the arm may not look at is one
line away.

**Two hard constraints on any selector that replaces this one.**

*Deterministic.* The same N candidates must select the same one every time,
because "the arm" has to be a fixed procedure for the comparison to mean
anything. A selector that consults a model, a clock, or an unordered set is a
second source of variance inside the arm.

*Blind.* It may not read the graded tests. The whole generation path withholds
the official ``test_patch`` and ``FAIL_TO_PASS`` ids and refuses to run any
other way, and a selector that recovered them -- by running the repository's
own test suite for the target ids, by reading the instance record, by asking
the model which patch passes the hidden tests -- would make this arm the only
one that saw its own grading. It may read what the candidates themselves
produced: their patches, their trees, their metrics, and tests the candidates
wrote.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: Which selector produced a selection. Written into every run's metrics, so a
#: batch collected under one rule can be told apart from a batch collected
#: under its replacement without going back to the commit.
SELECTOR_NAME = "lowest-tree-sha-placeholder-v1"

NO_CANDIDATE = "no_eligible_candidate"
ONLY_CANDIDATE = "only_eligible_candidate"
CANDIDATES_AGREE = "candidates_agree"
LOWEST_TREE_SHA = "lowest_tree_sha"


@dataclass(frozen=True)
class Candidate:
    """One of the N tries, as the generator got it back.

    ``tree`` is the Git tree the candidate's patch projects to when applied to
    the run's anonymous baseline (``project_candidate_patch``). It is ``None``
    when there is no such tree -- the candidate failed, or its patch does not
    apply cleanly -- which is the same fact as "this candidate has nothing
    submittable", stated once.
    """

    index: int
    patch: str
    tree: str | None
    metrics: Mapping[str, Any]


@dataclass(frozen=True)
class Selection:
    """Which candidate was chosen, by which clause, and whether that was a coin toss.

    ``arbitrary`` is the field this record exists for. It is true when the
    choice came down to the placeholder's ordering rule rather than to anything
    about the candidates, and counting it over a batch is the evidence for
    whether the placeholder is good enough: an arm whose choice is arbitrary in
    most of its runs is a one-third-budget single agent plus a lottery, and the
    paper cannot call it a fixed selector picking a best candidate.
    """

    index: int | None
    rule: str
    arbitrary: bool
    eligible: tuple[int, ...]
    distinct_trees: int
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "selector": SELECTOR_NAME,
            "selected_index": self.index,
            "decided_by": self.rule,
            "arbitrary_choice": self.arbitrary,
            "eligible_indices": list(self.eligible),
            "distinct_candidate_trees": self.distinct_trees,
            "note": self.note,
        }


def select_candidate(candidates: Sequence[Candidate]) -> Selection:
    """Pick one candidate. **Placeholder: this is not the paper's selector yet.**

    What it does, in order:

    1. Discard candidates with an empty patch and candidates whose patch does
       not project to a tree, which is this pipeline's form of "does not apply
       cleanly". Both are cases where there is nothing to submit, so no
       selector of any kind should keep them.
    2. If one survives, take it. If several survive and they all project to the
       *same* tree, take the lowest-numbered one: those candidates are the same
       patch, so the choice is not a choice.
    3. Otherwise take the candidate whose tree sha is lexicographically lowest,
       and record that as an arbitrary choice.

    **Why step 3 is not good enough for the arm the paper describes.** The
    paper's Best-of-N is "N non-communicating candidates; a fixed selector
    picks one", and a reader takes "picks" to mean the selector has a reason to
    prefer one candidate over another. A tree sha is a hash: ordering by it is
    a deterministic lottery, uncorrelated with anything about the patch. Under
    it the arm is a single agent on 1/N of the budget with N tickets, and the
    difference between it and the single-agent arm is an allocation difference
    plus noise -- which is a defensible thing to measure, but is not what "best
    of N" claims. Steps 1 and 2 do carry real content and would survive into
    any replacement.

    Note that the majority vote this rule replaced is worse, not better: at
    N=3, independently sampled at temperature 1.0, three candidates almost
    never project to the same tree, so a majority never forms and the rule
    degenerates to whatever its tie-break is -- while reading as though a
    majority decided.

    A replacement must be deterministic and must not read the graded tests; see
    this module's own docstring. The evidence for when one is needed is the
    ``arbitrary`` count over a finished batch.
    """
    eligible = tuple(
        candidate.index
        for candidate in candidates
        if candidate.patch.strip() and candidate.tree
    )
    by_index = {candidate.index: candidate for candidate in candidates}
    trees = {by_index[index].tree for index in eligible}

    if not eligible:
        return Selection(
            index=None,
            rule=NO_CANDIDATE,
            arbitrary=False,
            eligible=(),
            distinct_trees=0,
            note="no candidate produced a patch that projects to a tree",
        )
    if len(eligible) == 1:
        return Selection(
            index=eligible[0],
            rule=ONLY_CANDIDATE,
            arbitrary=False,
            eligible=eligible,
            distinct_trees=len(trees),
            note="one candidate had something to submit",
        )
    if len(trees) == 1:
        return Selection(
            index=min(eligible),
            rule=CANDIDATES_AGREE,
            arbitrary=False,
            eligible=eligible,
            distinct_trees=1,
            note="every eligible candidate projects to the same tree",
        )
    chosen = min(eligible, key=lambda index: (by_index[index].tree or "", index))
    return Selection(
        index=chosen,
        rule=LOWEST_TREE_SHA,
        arbitrary=True,
        eligible=eligible,
        distinct_trees=len(trees),
        note=(
            "the eligible candidates differ and the placeholder ordered them by "
            "tree sha, which is a hash and therefore a lottery"
        ),
    )


__all__ = [
    "CANDIDATES_AGREE",
    "LOWEST_TREE_SHA",
    "NO_CANDIDATE",
    "ONLY_CANDIDATE",
    "SELECTOR_NAME",
    "Candidate",
    "Selection",
    "select_candidate",
]
