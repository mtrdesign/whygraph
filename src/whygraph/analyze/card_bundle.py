"""What one rationale card shows: the evidence rings and the full-PR selection.

Plan 62 Part B (section 13.2). :func:`card_bundle` is the one pure
selection over a target's collected evidence (``collect_evidence(limit=20)``,
newest first) that decides, for a depth level,

* **Ring 0** - every line-level item, grouped into **units**: a squash
  commit together with the ``pr-origin`` commits recovered from its PR is
  one unit, dated and ordered by the squash commit. Below ``high`` the unit
  renders as its squash commit; from ``high`` its feature-branch commits
  render inside it.
* **Ring 1** - ``area`` items (commits that touched the file but not these
  lines), newest first, up to the level's count of context commits.
* **Full PRs** - the PRs rendered with body, roster, discussion and review
  threads: only a unit's **own** PR (its ``merge_commit_sha`` or
  ``head_sha`` is the unit's commit), of the units the level selects
  (``low``: units holding a ``blame`` item; ``medium`` and above: all
  units), in Ring 0 order, until stopper 1 - a count ceiling and a
  character budget, stopping at the first PR that does not fit. Other own
  PRs become title lines; PRs that hold a commit only through their roster
  are never shown in full.

No database, git or MCP access: the module imports neither ``mcp`` nor
``scan``, so the generator and the MCP tool share it. The per-level numbers
are module constants set by the plan's gate B0 (decision R45).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from whygraph.db.models import PullRequest

from .rationale import CommitEvidence

LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh")
"""The depth levels, shallowest first (plan 62 R24).

``high`` and ``xhigh`` stay defined for Part D but are not offered (R49):
:data:`~whygraph.core.config.OFFERED_DEPTH_LEVELS` is what a caller may ask for.
"""

DEFAULT_LEVEL = "medium"
"""The level a card is built at when its caller passes none (plan 62 Part C)."""

LINE_LEVEL_SOURCES = frozenset(
    {"blame", "pr-origin", "blame-walked", "predecessor-blame", "local"}
)
"""Evidence sources that attribute the target's own lines: Ring 0."""

FULL_PR_MAX_COUNT: Mapping[str, int] = {
    "low": 16,
    "medium": 16,
    "high": 16,
    "xhigh": 16,
}
"""Stopper 1's count ceiling: full PR blocks in one card (R45)."""

FULL_PR_MAX_CHARS: Mapping[str, int] = {
    "low": 35_000,
    "medium": 40_000,
    "high": 40_000,
    "xhigh": 40_000,
}
"""Stopper 1's character budget: full PR block text in one card (R45)."""

TOTAL_MAX_CHARS: Mapping[str, int] = {
    "low": 158_000,
    "medium": 163_000,
    "high": 183_000,
    "xhigh": 183_000,
}
"""Stopper 2's total prompt cap in characters (R45; ``xhigh`` as ``high``)."""

CONTEXT_COMMITS: Mapping[str, int] = {
    "low": 0,
    "medium": 4,
    "high": 6,
    "xhigh": 8,
}
"""Ring 1 commits (compact context) a card may show (R45)."""

FEATURE_COMMITS_FROM = "high"
"""The shallowest level whose squash units show their feature-branch commits."""

SOURCES = ("included", "omitted", "unavailable")
"""The values of :attr:`CardCoverage.source` (plan 62 section 14.2a).

``included``: the draft prompt held the target's source; ``omitted``: the
source is longer than the draft's limit, so the prompt held none;
``unavailable``: the target's lines could not be read.
"""


@dataclass(frozen=True, slots=True)
class LevelCaps:
    """The numbers of one depth level.

    Attributes
    ----------
    full_pr_max_count : int
        Stopper 1's count ceiling.
    full_pr_max_chars : int
        Stopper 1's character budget over full PR block text.
    total_max_chars : int
        Stopper 2's total prompt cap, in characters.
    context_commits : int
        Ring 1 commits the level may show.
    feature_commits : bool
        Whether squash units show their feature-branch commits.
    """

    full_pr_max_count: int
    full_pr_max_chars: int
    total_max_chars: int
    context_commits: int
    feature_commits: bool


def _running_max(table: Mapping[str, int], level: str) -> int:
    """``table[level]``, raised to the largest value of any shallower level (rule 3b)."""
    upto = LEVELS[: LEVELS.index(level) + 1]
    return max(table[name] for name in upto)


def level_caps(level: str) -> LevelCaps:
    """The numbers of ``level``, non-decreasing with depth by construction.

    Each value is the maximum of the level's constant and every shallower
    level's (plan 62 section 13.3 rule 3b), so tuning one constant can
    never make a deeper level show less.

    Parameters
    ----------
    level : str
        One of :data:`LEVELS`.

    Returns
    -------
    LevelCaps
        The level's ceilings, budgets and context count.

    Raises
    ------
    ValueError
        If ``level`` is not one of :data:`LEVELS`.
    """
    if level not in LEVELS:
        raise ValueError(f"unknown depth level {level!r}; expected one of {LEVELS}")
    return LevelCaps(
        full_pr_max_count=_running_max(FULL_PR_MAX_COUNT, level),
        full_pr_max_chars=_running_max(FULL_PR_MAX_CHARS, level),
        total_max_chars=_running_max(TOTAL_MAX_CHARS, level),
        context_commits=_running_max(CONTEXT_COMMITS, level),
        feature_commits=LEVELS.index(level) >= LEVELS.index(FEATURE_COMMITS_FROM),
    )


@dataclass(frozen=True, slots=True)
class Unit:
    """One Ring 0 unit: a commit, plus a squash commit's recovered originals.

    Attributes
    ----------
    head : CommitEvidence
        The unit's commit (a squash commit for a squash unit); it dates and
        orders the unit.
    members : tuple[CommitEvidence, ...]
        The ``pr-origin`` items recovered from the squash commit's PR, in
        evidence order; empty for any other unit.
    own_prs : tuple[PullRequest, ...]
        The PRs whose ``merge_commit_sha`` or ``head_sha`` is ``head``'s
        commit, in ``head.pull_requests`` order.
    """

    head: CommitEvidence
    members: tuple[CommitEvidence, ...] = ()
    own_prs: tuple[PullRequest, ...] = ()

    @property
    def holds_blame(self) -> bool:
        """Whether the unit holds a ``blame`` item (``low``'s full-PR rule)."""
        return any(i.source == "blame" for i in (self.head, *self.members))


@dataclass(frozen=True, slots=True)
class CardBundle:
    """What one card at one level includes (:func:`card_bundle`).

    Attributes
    ----------
    level : str
        The depth level the bundle was selected for.
    ring0 : tuple[CommitEvidence, ...]
        The Ring 0 commits the card renders, in rendering order (the
        evidence's order, a squash unit's feature-branch commits after its
        squash commit).
    ring1 : tuple[CommitEvidence, ...]
        The context candidates, newest first, at most the level's count;
        the generator's context budget may render fewer.
    ring0_all : tuple[CommitEvidence, ...]
        Every line-level item collected, whatever the level renders: what
        review-thread ownership (R22) is checked against.
    full_prs : tuple[PullRequest, ...]
        The PRs rendered in full, in Ring 0 priority order.
    title_prs : tuple[PullRequest, ...]
        Own PRs rendered as one title line, in Ring 0 priority order: the
        ones stopper 1 stopped and the ones the level's rule did not select.
    demoted : tuple[PullRequest, ...]
        The own PRs the level's rule selected but stopper 1 stopped (a
        subset of ``title_prs``).
    units : tuple[Unit, ...]
        The Ring 0 units, in priority order.
    own : Mapping[str, frozenset[int]]
        Per rendered commit SHA, the numbers of the PRs that are its own
        (a squash unit's PR counts as its feature-branch commits' own).
    context_total : int
        ``area`` items collected (the context the card could have shown).
    caps : LevelCaps
        The level's numbers.
    """

    level: str
    ring0: tuple[CommitEvidence, ...]
    ring1: tuple[CommitEvidence, ...]
    ring0_all: tuple[CommitEvidence, ...]
    full_prs: tuple[PullRequest, ...]
    title_prs: tuple[PullRequest, ...]
    demoted: tuple[PullRequest, ...]
    units: tuple[Unit, ...]
    own: Mapping[str, frozenset[int]]
    context_total: int
    caps: LevelCaps

    @property
    def items(self) -> tuple[CommitEvidence, ...]:
        """Every commit the card may include: Ring 0, then Ring 1.

        What the description backfill describes, and what a card's
        ``evidence_count`` and ``evidence`` refs are taken from.
        """
        return (*self.ring0, *self.ring1)


def _parse_time(value: object) -> datetime | None:
    """An ISO-8601 timestamp as an aware datetime (naive read as UTC), else ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _own_prs(item: CommitEvidence) -> tuple[PullRequest, ...]:
    """The PRs of ``item`` whose merge or head commit is ``item``'s commit."""
    sha = item.commit.sha
    return tuple(
        pr
        for pr in item.pull_requests
        if pr.merge_commit_sha == sha or pr.head_sha == sha
    )


def _units(line_items: Sequence[CommitEvidence]) -> list[Unit]:
    """Group line-level items into units, in evidence order of their heads.

    A ``pr-origin`` item joins the first squash commit in the evidence
    whose PR (its ``merge_commit_sha`` is that commit) it is linked to;
    one with no such commit in the evidence is a unit of its own.
    """
    squash_of: dict[int, int] = {}
    for index, item in enumerate(line_items):
        if item.source == "pr-origin":
            continue
        for pr in item.pull_requests:
            if pr.merge_commit_sha == item.commit.sha:
                squash_of.setdefault(pr.number, index)
    members: dict[int, list[CommitEvidence]] = {}
    joined: set[int] = set()
    for index, item in enumerate(line_items):
        if item.source != "pr-origin":
            continue
        head = next(
            (
                squash_of[pr.number]
                for pr in item.pull_requests
                if pr.number in squash_of
            ),
            None,
        )
        if head is not None:
            members.setdefault(head, []).append(item)
            joined.add(index)
    return [
        Unit(item, tuple(members.get(index, ())), _own_prs(item))
        for index, item in enumerate(line_items)
        if index not in joined
    ]


def _priority(units: Sequence[Unit]) -> list[Unit]:
    """Ring 0 order: ``blame`` units newest first, the oldest dated unit, the rest newest first."""
    floor = datetime.min.replace(tzinfo=timezone.utc)

    def newest_first(group: Sequence[Unit]) -> list[Unit]:
        return sorted(
            group,
            key=lambda u: _parse_time(u.head.commit.committed_at) or floor,
            reverse=True,
        )

    ordered = newest_first([u for u in units if u.holds_blame])
    dated = [u for u in units if _parse_time(u.head.commit.committed_at)]
    if dated:
        # The oldest; on a tie the later one in evidence order (older there).
        oldest = min(
            reversed(dated), key=lambda u: _parse_time(u.head.commit.committed_at)
        )
        if all(u is not oldest for u in ordered):
            ordered.append(oldest)
    ordered.extend(newest_first([u for u in units if all(u is not o for o in ordered)]))
    return ordered


def card_bundle(
    evidence: Sequence[CommitEvidence],
    level: str = DEFAULT_LEVEL,
    caps: LevelCaps | None = None,
    *,
    pr_chars: Callable[[PullRequest], int] | None = None,
) -> CardBundle:
    """Select what one card at ``level`` includes (plan 62 section 13.2).

    Every line-level unit is in Ring 0 at every level. A level differs only
    in whose PRs are shown in full, whether squash units show their
    feature-branch commits, and how many context commits it may show.
    Stopper 1 runs here, on PR text only: full PR blocks are added in Ring 0
    order until the count ceiling or the character budget binds, and adding
    stops at the first block that does not fit. Stopper 2 (the total prompt
    cap) and the context character budget need the described commits and
    run in the generator.

    Parameters
    ----------
    evidence : Sequence[CommitEvidence]
        The target's collected evidence, newest first.
    level : str, optional
        One of :data:`LEVELS`; default :data:`DEFAULT_LEVEL`.
    caps : LevelCaps, optional
        The level's numbers; ``None`` (default) uses :func:`level_caps`.
    pr_chars : callable, optional
        The characters one PR's full block takes in the prompt; ``None``
        (default) counts its title, body, roster headlines and comment
        bodies. The generator passes its own renderer's length.

    Returns
    -------
    CardBundle
        The rings, the units and the full / title-line PR selection.

    Raises
    ------
    ValueError
        If ``level`` is not one of :data:`LEVELS`.
    """
    caps = caps if caps is not None else level_caps(level)
    measure = pr_chars if pr_chars is not None else _estimated_pr_chars
    line_items = [i for i in evidence if i.source in LINE_LEVEL_SOURCES]
    area = [i for i in evidence if i.source not in LINE_LEVEL_SOURCES]
    units = _units(line_items)
    priority = _priority(units)
    low_rule = LEVELS.index(level) < LEVELS.index("medium")

    full: list[PullRequest] = []
    title: list[PullRequest] = []
    demoted: list[PullRequest] = []
    seen: set[int] = set()
    used = 0
    stopped = False
    for unit in priority:
        selected = unit.holds_blame or not low_rule
        for pr in unit.own_prs:
            if pr.number in seen:
                continue
            seen.add(pr.number)
            if not selected:
                title.append(pr)
                continue
            size = measure(pr)
            if (
                stopped
                or len(full) >= caps.full_pr_max_count
                or used + size > caps.full_pr_max_chars
            ):
                stopped = True
                title.append(pr)
                demoted.append(pr)
                continue
            full.append(pr)
            used += size
    # The title lines in Ring 0 priority order, whichever reason put them there.
    rank: dict[int, int] = {}
    for pr in (p for u in priority for p in u.own_prs):
        rank.setdefault(pr.number, len(rank))
    title.sort(key=lambda pr: rank[pr.number])

    ring0: list[CommitEvidence] = []
    own: dict[str, frozenset[int]] = {}
    for unit in units:
        unit_prs = frozenset(pr.number for pr in unit.own_prs)
        ring0.append(unit.head)
        own[unit.head.commit.sha] = unit_prs
        if caps.feature_commits:
            for member in unit.members:
                ring0.append(member)
                own[member.commit.sha] = unit_prs | {
                    pr.number for pr in _own_prs(member)
                }
    return CardBundle(
        level=level,
        ring0=tuple(ring0),
        ring1=tuple(area[: caps.context_commits]),
        ring0_all=tuple(line_items),
        full_prs=tuple(full),
        title_prs=tuple(title),
        demoted=tuple(demoted),
        units=tuple(priority),
        own=own,
        context_total=len(area),
        caps=caps,
    )


def _estimated_pr_chars(pr: PullRequest) -> int:
    """A PR block's size without the renderer: title, body, roster and comment text."""
    size = len(pr.title or "") + len(pr.body or "")
    for raw in (pr.commit_titles, pr.comments):
        try:
            entries = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            entries = []
        for entry in entries if isinstance(entries, list) else ():
            if isinstance(entry, dict):
                size += len(str(entry.get("headline") or entry.get("body") or ""))
    return size


@dataclass(frozen=True, slots=True)
class CardCoverage:
    """What a card left out, and why (plan 62 sections 13.2 and 13.4).

    Built by the generator once both stoppers and the context budget ran.

    Attributes
    ----------
    line_commits : int
        Ring 0 commits the prompt rendered.
    context_commits : int
        Ring 1 commits the prompt rendered.
    context_omitted : int
        ``area`` commits collected but not rendered (the level's count, the
        context character budget and stopper 2 together).
    full_prs_demoted : int
        Own PRs the level selected for a full block but that render as a
        title line (stopper 1 and stopper 2).
    threads_lost : int
        Demoted PRs that had at least one qualified review thread.
    trimmed : tuple[str, ...]
        What the stoppers did, in order: ``"full_pr_budget"`` (stopper 1
        demoted PRs), ``"prompt_cap_context"`` / ``"prompt_cap_full_prs"``
        (stopper 2 removed context commits / demoted full PRs) and
        ``"ring0_over_cap"`` (still over the cap with only Ring 0 left).
    descriptions_missing : int
        Rendered commits with no description (``Summary: (no description:
        ...)``).
    omitted_shas : frozenset[str]
        The bundle's Ring 1 SHAs the prompt did not render; never served,
        used to keep them out of a card's deferred SHAs.
    source : str or None
        Whether the draft prompt held the target's source (:data:`SOURCES`);
        ``None`` when the generator was built without it.
    """

    line_commits: int = 0
    context_commits: int = 0
    context_omitted: int = 0
    full_prs_demoted: int = 0
    threads_lost: int = 0
    trimmed: tuple[str, ...] = ()
    descriptions_missing: int = 0
    omitted_shas: frozenset[str] = frozenset()
    source: str | None = None

    def as_dict(self) -> dict[str, object]:
        """The card's ``coverage`` object (every field but ``omitted_shas``).

        ``source`` is present only when set.
        """
        out: dict[str, object] = {
            "full_prs_demoted": self.full_prs_demoted,
            "threads_lost": self.threads_lost,
            "trimmed": list(self.trimmed),
            "descriptions_missing": self.descriptions_missing,
            "line_commits": self.line_commits,
            "context_commits": self.context_commits,
            "context_omitted": self.context_omitted,
        }
        if self.source is not None:
            out["source"] = self.source
        return out
