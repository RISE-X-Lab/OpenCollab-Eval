"""Draw the frozen task suite the main grid is run on.

The suite is not a convenience sample: the paper reports a paired contrast per
task, so which tasks were drawn -- and in what order -- is part of the claim.
Three properties are therefore built in rather than left to the caller.

* **The draw is a pure function of (frame, seed).** No process-local random
  state is used. Each candidate is ranked by ``sha256(f"{namespace}:{seed}:{id}")``
  and the smallest keys win, so the same frame and seed reproduce the same
  files byte for byte on any Python, any platform, any run order. A
  ``random.Random`` seeded once would also be deterministic today, but its
  output is a property of the interpreter's generator rather than of the
  pre-registration, and the pre-registration is the thing that has to survive.
* **One repository cannot carry the suite.** SWE-bench Verified is 46% one
  repository; drawn in proportion to the frame, half the suite would be
  ``django`` and every per-repository reading would be a reading of it. Shares
  are therefore capped and the excess redistributed proportionally over the
  rest, iterating because redistribution can push a second repository over the
  cap.
* **Every draw is longer than the part that is used now, and the part used now
  is a prefix.** Two different pressures need this. A container image that will
  not start is not a random event, so its replacement cannot be chosen after the
  fact: the suite draw is an ordered 110 and the suite is the first 100 that
  pass pre-flight, each skip recorded with its reason. And a grid that is
  extended later must extend the frozen list rather than redraw it: the
  replication subset is an ordered 50 whose first 30 are the ones run first, so
  growing the subset appends and never rewrites.

  Both are the same construction. A head block is drawn under the capped,
  stratified rule; a tail block is drawn the same way from what is left; the
  ordered list is the head block, shuffled, followed by the tail block,
  shuffled. Shuffling the two blocks together would break the property that
  makes the head usable on its own -- the head would stop being a stratified
  draw and become a random prefix of one.

Difficulty stratification happens inside each repository, over the benchmark's
own annotated ``difficulty`` field. The allocation is Hamilton's largest
remainder throughout, with ties broken by name so that the result does not
depend on dictionary order.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

__all__ = [
    "FrameRow",
    "SuiteDraw",
    "allocate_by_largest_remainder",
    "capped_repository_shares",
    "draw_ordered_list",
    "stratum_counts",
]


@dataclass(frozen=True)
class FrameRow:
    """One instance of the sampling frame, reduced to what the draw reads."""

    instance_id: str
    repo: str
    difficulty: str


@dataclass(frozen=True)
class SuiteDraw:
    """An ordered draw plus the allocation it was produced from."""

    ordered: tuple[str, ...]
    repository_allocation: Mapping[str, int] = field(default_factory=dict)
    stratum_allocation: Mapping[tuple[str, str], int] = field(default_factory=dict)


def _rank_key(namespace: str, seed: int, instance_id: str) -> str:
    return hashlib.sha256(f"{namespace}:{seed}:{instance_id}".encode()).hexdigest()


def capped_repository_shares(counts: Mapping[str, int], cap: float) -> dict[str, float]:
    """Frame shares with no repository above ``cap``, the excess redistributed.

    Repositories over the cap are pinned to it and the remaining mass is split
    among the rest in proportion to their frame counts. Pinning one repository
    can lift another over the cap, so this repeats until no unpinned share
    exceeds it. ``cap`` at or below ``1 / len(counts)`` cannot be satisfied and
    is rejected rather than silently returning an uncapped draw.
    """
    if not counts:
        return {}
    total = sum(counts.values())
    if total <= 0:
        raise ValueError("the frame is empty")
    if not 0.0 < cap <= 1.0:
        raise ValueError("cap must lie in (0, 1]")
    if cap * len(counts) < 1.0:
        raise ValueError(f"cap {cap} cannot be met by {len(counts)} repositories")
    pinned: dict[str, float] = {}
    while True:
        free = {name: value for name, value in counts.items() if name not in pinned}
        free_mass = 1.0 - sum(pinned.values())
        free_total = sum(free.values())
        if free_total <= 0:
            break
        shares = {name: free_mass * value / free_total for name, value in free.items()}
        over = sorted(name for name, share in shares.items() if share > cap + 1e-12)
        if not over:
            return {**pinned, **shares}
        for name in over:
            pinned[name] = cap
    return dict(pinned)


def allocate_by_largest_remainder(
    shares: Mapping[str, float],
    total: int,
    *,
    available: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """Split ``total`` seats over ``shares``, never more than ``available``.

    Hamilton's method: floor every quota, then hand the remaining seats to the
    largest fractional parts, ties broken by name. A stratum that runs out of
    instances is capped at what it has and its unclaimed seats go round again,
    so the allocation always sums to ``total`` when the frame is large enough.

    When ``available`` is given, a name missing from it has nothing left and is
    allocated nothing. Reading a missing name as unbounded instead is the
    failure this signature is written against: the reserve is allocated against
    what the suite block did not consume, and a repository the suite block
    exhausted would otherwise be handed seats that then silently go unfilled,
    leaving a draw shorter than the one that was pre-registered.
    """
    if total < 0:
        raise ValueError("total must not be negative")
    limits = (
        dict.fromkeys(shares, total)
        if available is None
        else {name: available.get(name, 0) for name in shares}
    )
    allocation = dict.fromkeys(shares, 0)
    remaining = total
    active = {name for name, share in shares.items() if share > 0 and limits[name] > 0}
    while remaining > 0 and active:
        mass = sum(shares[name] for name in active)
        if mass <= 0:
            break
        quotas = {name: remaining * shares[name] / mass for name in active}
        awarded = {name: min(int(quotas[name]), limits[name] - allocation[name]) for name in active}
        seats_left = remaining - sum(awarded.values())
        order = sorted(active, key=lambda name: (-(quotas[name] - int(quotas[name])), name))
        for name in order:
            if seats_left <= 0:
                break
            if allocation[name] + awarded[name] < limits[name]:
                awarded[name] += 1
                seats_left -= 1
        if not any(awarded.values()):
            break
        for name, seats in awarded.items():
            allocation[name] += seats
        remaining = seats_left
        active = {name for name in active if allocation[name] < limits[name]}
    return allocation


def stratum_counts(rows: Iterable[FrameRow]) -> dict[tuple[str, str], int]:
    """How many frame instances sit in each (repository, difficulty) cell."""
    return dict(Counter((row.repo, row.difficulty) for row in rows))


def _select(
    rows: Sequence[FrameRow],
    *,
    seed: int,
    namespace: str,
    repository_allocation: Mapping[str, int],
) -> tuple[list[str], dict[tuple[str, str], int]]:
    by_repo: dict[str, list[FrameRow]] = {}
    for row in rows:
        by_repo.setdefault(row.repo, []).append(row)
    picked: list[str] = []
    stratum_allocation: dict[tuple[str, str], int] = {}
    for repo in sorted(repository_allocation):
        seats = repository_allocation[repo]
        candidates = by_repo.get(repo, [])
        if seats <= 0 or not candidates:
            continue
        available = Counter(row.difficulty for row in candidates)
        shares = {name: value / len(candidates) for name, value in available.items()}
        per_difficulty = allocate_by_largest_remainder(shares, seats, available=available)
        for difficulty in sorted(per_difficulty):
            take = per_difficulty[difficulty]
            if take <= 0:
                continue
            stratum_allocation[(repo, difficulty)] = take
            pool = sorted(
                (row.instance_id for row in candidates if row.difficulty == difficulty),
                key=lambda instance_id: _rank_key(namespace, seed, instance_id),
            )
            picked.extend(pool[:take])
    return picked, stratum_allocation


def draw_ordered_list(
    rows: Sequence[FrameRow],
    *,
    seed: int,
    head_size: int,
    total_size: int,
    cap: float = 0.30,
    namespace: str = "suite",
) -> SuiteDraw:
    """An ordered draw of ``total_size`` whose first ``head_size`` stand alone.

    The head block is a capped, difficulty-stratified draw in its own right; the
    tail block is drawn the same way from what is left, under the *frame's*
    capped shares rather than the residual's, so that a repository the head
    block exhausted does not have its share handed to whichever repository
    happens to have instances left.

    ``namespace`` separates one draw from another under the same seed. Two draws
    over overlapping populations with the same namespace would rank a shared
    instance identically, so the second draw would inherit the first draw's
    ordering; the suite draw and the subset draw therefore do not share one.
    """
    if total_size < head_size:
        raise ValueError("total_size must be at least head_size")
    counts = Counter(row.repo for row in rows)
    shares = capped_repository_shares(counts, cap)
    head_allocation = allocate_by_largest_remainder(shares, head_size, available=counts)
    head, head_strata = _select(rows, seed=seed, namespace=namespace, repository_allocation=head_allocation)
    chosen = set(head)
    rest = [row for row in rows if row.instance_id not in chosen]
    tail_counts = Counter(row.repo for row in rest)
    tail_allocation = allocate_by_largest_remainder(shares, total_size - head_size, available=tail_counts)
    tail, tail_strata = _select(rest, seed=seed, namespace=f"{namespace}-tail", repository_allocation=tail_allocation)
    ordered = [
        *sorted(head, key=lambda instance_id: _rank_key(f"{namespace}-order", seed, instance_id)),
        *sorted(tail, key=lambda instance_id: _rank_key(f"{namespace}-tail-order", seed, instance_id)),
    ]
    allocation = Counter(head_allocation)
    allocation.update(tail_allocation)
    strata = Counter(head_strata)
    strata.update(tail_strata)
    return SuiteDraw(tuple(ordered), dict(allocation), dict(strata))
