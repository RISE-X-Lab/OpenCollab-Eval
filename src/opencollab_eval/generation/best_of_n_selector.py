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

#: Persist the selector version with every run's metrics.
SELECTOR_NAME = "lowest-tree-sha-placeholder-v1"

NO_CANDIDATE = "no_eligible_candidate"
ONLY_CANDIDATE = "only_eligible_candidate"
CANDIDATES_AGREE = "candidates_agree"
LOWEST_TREE_SHA = "lowest_tree_sha"


@dataclass(frozen=True)
class Candidate:
    """One candidate and the tree its patch projects to on the anonymous baseline.

    ``tree=None`` indicates that no submittable tree was produced.
    """

    index: int
    patch: str
    tree: str | None
    metrics: Mapping[str, Any]


@dataclass(frozen=True)
class Selection:
    """The selected candidate and the reason for the choice.

    ``arbitrary`` marks selection by the placeholder tree ordering, allowing a
    batch to report how often the rule lacked a quality-based preference.
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
    """Select an eligible patch using the deterministic placeholder rule.

    Equivalent trees select the lowest candidate index. Distinct trees select
    the lowest tree SHA and mark the choice arbitrary because hash order carries
    no patch-quality evidence. Count these choices when assessing the selector.
    Any replacement must remain deterministic and blind to graded tests.
    """
    eligible = tuple(
        candidate.index
        for candidate in candidates
        if candidate.patch.strip() and candidate.tree
        and candidate.metrics.get("submission_eligible") is not False
        and not candidate.metrics.get("error")
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
