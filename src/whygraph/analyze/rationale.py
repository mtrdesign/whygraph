"""Value objects for the rationale generator.

* :class:`CommitEvidence` — the *input*: one scanned commit paired with the
  pull requests and issues linked to it. The caller groups these (the
  generator does no database joins); a sequence of them is the evidence
  bundle the generator explains.
* :class:`Rationale` — the *output*: a structured "why this code exists"
  card returned by :meth:`whygraph.analyze.RationaleGenerator.generate`.
* :class:`Item` and :class:`ItemRef` — one constraint or risk of a card,
  with the evidence it names and how well that evidence supports it.
* :class:`ItemCheck` - the verification pass's verdict on one item.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from whygraph.db.models import Commit, Issue, PullRequest

if TYPE_CHECKING:
    from .card_bundle import CardCoverage

ITEM_BASES = ("quoted", "cited", "inferred", "unknown")
"""The values of :attr:`Item.basis`, strongest first."""

REF_KINDS = ("commit", "pr", "issue")
"""The values of :attr:`ItemRef.kind`."""

VERDICTS = ("holds", "contradicted", "cannot_tell")
"""The values of :attr:`ItemCheck.verdict` (plan 62 section 14.2)."""


@dataclass(frozen=True, slots=True)
class CommitEvidence:
    """One scanned commit plus the pull requests and issues linked to it.

    Assembled by the caller — the rationale generator performs no database
    access, so grouping the linked pull requests and issues onto a commit is
    the caller's responsibility. A sequence of these is the evidence bundle
    handed to :meth:`whygraph.analyze.RationaleGenerator.generate`.

    Attributes
    ----------
    commit : Commit
        The scanned commit row — carries the author, subject, body, and the
        optional ``llm_description`` diff summary.
    pull_requests : tuple[PullRequest, ...]
        Pull requests linked to ``commit``. Empty when none are linked.
    issues : tuple[Issue, ...]
        Issues linked to ``commit`` (via its pull requests). Empty when none
        are linked.
    source : str
        How this evidence row was discovered. One of:

        * ``"blame"`` — line-level attribution from the target's current
          range (highest-precision signal).
        * ``"pr-origin"`` — an original feature-branch commit recovered
          from a squash-merged PR: when the queried lines blame to a
          squash commit, they are re-blamed at the PR's ``head_sha`` so
          each line maps back to the commit that actually authored it.
        * ``"blame-walked"`` — surfaced only after blame walked past a
          refactor-heavy commit. Still line-level, but one or more boring
          commits were skipped to reach this author.
        * ``"predecessor-blame"`` — line-level attribution inside a
          rename predecessor of the current file, at the commit just
          before the rename event.
        * ``"area"`` — drawn from the ``commit_file_change`` index
          (touched the file or a rename ancestor, but not these specific
          lines). Weaker than blame, reaches deleted-predecessor history
          that blame cannot.

        Older callers that construct :class:`CommitEvidence` directly
        without specifying ``source`` get the default ``"blame"`` — which
        matches the pre-Phase-3 behaviour where every entry came from
        ``git blame``.
    lines : tuple[tuple[int, int], ...] or None
        Working-tree line ranges (1-based, inclusive) the commit owns within
        the queried range. Set only on ``"blame"`` items; ``None`` when the
        coordinates are not the working tree's (walked, origin, predecessor
        and area items).
    """

    commit: Commit
    pull_requests: tuple[PullRequest, ...] = ()
    issues: tuple[Issue, ...] = ()
    source: str = "blame"
    lines: tuple[tuple[int, int], ...] | None = None


@dataclass(frozen=True, slots=True)
class ItemRef:
    """One piece of evidence an :class:`Item` names as its source.

    Attributes
    ----------
    kind : str
        ``"commit"``, ``"pr"`` or ``"issue"`` (:data:`REF_KINDS`).
    id : str
        The commit's full SHA, or the pull request's or issue's number.
    url : str or None
        The pull request's or issue's ``html_url``; ``None`` for a commit
        and when the evidence has none.
    """

    kind: str
    id: str
    url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """The ref as ``{"kind", "id"}`` plus ``"url"`` when it has one."""
        out: dict[str, Any] = {"kind": self.kind, "id": self.id}
        if self.url:
            out["url"] = self.url
        return out


@dataclass(frozen=True, slots=True)
class ItemCheck:
    """The verification pass's verdict on one constraint or risk (plan 62 section 14.2).

    A verdict is as of the target's source when the card was built: a
    committed edit of the lines changes the card's evidence and regenerates
    it, an uncommitted one does not.

    Attributes
    ----------
    verdict : str
        One of :data:`VERDICTS`: ``"holds"`` (the source shows the claim is
        true of today's code), ``"contradicted"`` (it shows the claim no
        longer is) or ``"cannot_tell"`` (the source alone does not settle
        it, or the verifier gave no verdict).
    reason : str
        The verifier's one-line reason; may be empty.
    """

    verdict: str
    reason: str = ""

    def as_dict(self) -> dict[str, str]:
        """The check as ``{"verdict", "reason"}``."""
        return {"verdict": self.verdict, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class Item:
    """One constraint or risk of a :class:`Rationale`, with its source.

    ``basis`` is assigned by code, never by the model
    (:func:`whygraph.analyze.rationale_generator._item`): the model names
    refs and a quote, and the generator checks them against the evidence
    bundle.

    Attributes
    ----------
    text : str
        The constraint or the risk.
    basis : str
        One of :data:`ITEM_BASES`. ``"quoted"``: a cited source holds
        ``quote``. ``"cited"``: a source in the bundle is named, but holds
        no quote. ``"inferred"``: no source in the bundle is named, so the
        item is the model's reading of the evidence. ``"unknown"``: the
        card predates sources (an old cache row or bundle).
    refs : tuple[ItemRef, ...]
        The named sources that are in the evidence bundle. Empty for
        ``"inferred"`` and ``"unknown"``.
    quote : str
        The checked quote, whitespace runs collapsed to one space. Empty
        unless ``basis`` is ``"quoted"``.
    check : ItemCheck or None
        The verification pass's verdict (a ``high`` card and deeper);
        ``None`` when the item was not checked.
    """

    text: str
    basis: str
    refs: tuple[ItemRef, ...] = ()
    quote: str = ""
    check: ItemCheck | None = None

    def as_dict(self) -> dict[str, Any]:
        """The item as ``{"text", "basis", "refs"}`` plus ``"quote"`` and ``"check"`` when set.

        The shape the cache stores and the card serves; a plain string
        decodes back as basis ``"unknown"``.
        """
        out: dict[str, Any] = {
            "text": self.text,
            "basis": self.basis,
            "refs": [ref.as_dict() for ref in self.refs],
        }
        if self.quote:
            out["quote"] = self.quote
        if self.check is not None:
            out["check"] = self.check.as_dict()
        return out


@dataclass(frozen=True, slots=True)
class Rationale:
    """One LLM-written explanation of why a piece of code exists.

    Returned by :meth:`whygraph.analyze.RationaleGenerator.generate`. The
    three list-shaped fields are tuples so the dataclass stays immutable; an
    empty tuple means the evidence supported no entry.

    Attributes
    ----------
    purpose : str
        One sentence stating what the code does today.
    why : str
        A short paragraph of historical and contextual rationale drawn from
        the evidence bundle.
    constraints : tuple[Item, ...]
        Invariants the next editor must preserve, each with its source.
    tradeoffs : tuple[str, ...]
        Notable design decisions visible in the evidence.
    risks : tuple[Item, ...]
        Risks of modifying this code, each with its source.
    model : str
        Model identifier as reported by the provider — echoed from
        :attr:`CompletionResponse.model`.
    provider : str
        Provider tag (``"anthropic"``, ``"openai"``, …) from
        :attr:`CompletionResponse.provider`.
    input_tokens : int or None
        Prompt-token count when the provider reports it.
    output_tokens : int or None
        Completion-token count when the provider reports it.
    coverage : CardCoverage or None
        What the card's prompt left out (plan 62 section 13.4), set by
        :meth:`~whygraph.analyze.RationaleGenerator.generate` and stored with
        the cached card; ``None`` for a legacy cache row, which has none.
    """

    purpose: str
    why: str
    constraints: tuple[Item, ...]
    tradeoffs: tuple[str, ...]
    risks: tuple[Item, ...]
    model: str
    provider: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    coverage: CardCoverage | None = None
