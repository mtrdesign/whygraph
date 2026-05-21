"""Value objects for the rationale generator.

* :class:`CommitEvidence` — the *input*: one scanned commit paired with the
  pull requests and issues linked to it. The caller groups these (the
  generator does no database joins); a sequence of them is the evidence
  bundle the generator explains.
* :class:`Rationale` — the *output*: a structured "why this code exists"
  card returned by :meth:`whygraph.analyze.RationaleGenerator.generate`.
"""

from __future__ import annotations

from dataclasses import dataclass

from whygraph.db.models import Commit, Issue, PullRequest


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
    """

    commit: Commit
    pull_requests: tuple[PullRequest, ...] = ()
    issues: tuple[Issue, ...] = ()


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
    constraints : tuple[str, ...]
        Invariants the next editor must preserve.
    tradeoffs : tuple[str, ...]
        Notable design decisions visible in the evidence.
    risks : tuple[str, ...]
        Risks of modifying this code.
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
    """

    purpose: str
    why: str
    constraints: tuple[str, ...]
    tradeoffs: tuple[str, ...]
    risks: tuple[str, ...]
    model: str
    provider: str
    input_tokens: int | None = None
    output_tokens: int | None = None
