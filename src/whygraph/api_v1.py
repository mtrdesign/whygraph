"""Wire models of the platform's versioned ``/api/v1`` (M2e).

One module, imported by both sides: the platform validates request bodies
with the ``*In`` models and shapes its answers with the ``*Out`` models;
a connected local portal builds the requests and validates the answers
with the same classes. It imports nothing from :mod:`whygraph.portal`, so
the client side never pulls the platform in.

Request models are strict (unknown fields refused, no type coercion) and
validate everything that reaches ``git``: a hunk's SHA and path sit in a
``git blame`` argv before ``--``, so an option-like value must never get
through. Response models ignore unknown fields, so the platform can add a
field without breaking an older client, and mirror the dict shapes of
:mod:`whygraph.mcp.evidence` and :mod:`whygraph.mcp.rationale` exactly.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictInt,
    StrictStr,
    field_validator,
    model_serializer,
    model_validator,
)

from whygraph.services.git import BlameHunk, BlameOrigin

API_VERSION = 1
"""The ``/api/v1`` contract version."""

MAX_HUNKS = 200
"""Most hunks one request may carry."""

MAX_ORIGINS = 200
"""Most origin ranges one hunk may carry."""

MAX_LINE = 1_000_000
"""Highest line number a range may name."""

MAX_PATH = 4096
"""Longest path, in characters."""

MAX_QUALIFIED_NAME = 1024
"""Longest ``qualified_name``, in characters."""

MAX_LIMIT = 50
"""Highest evidence ``limit``."""

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

Source = Literal["blame", "pr-origin", "blame-walked", "predecessor-blame", "area"]
"""An evidence item's ``source`` label (see ``mcp.evidence._SOURCE_PRIORITY``)."""

PushStatus = Literal[
    "uncommitted", "not_pushed", "not_on_default_branch", "pending_scan"
]
"""Why a connected portal could not get evidence for a SHA from the platform."""

Role = Literal["owner", "admin", "member"]
"""The token user's role in the project's org (never ``reader``)."""


def _check_sha(value: str) -> str:
    """Refuse anything but a full lowercase hex SHA (not git's all-zero one)."""
    if not _SHA.fullmatch(value):
        raise ValueError("sha must be 40 or 64 lowercase hex characters")
    if set(value) == {"0"}:
        raise ValueError("sha must not be the all-zero (uncommitted) SHA")
    return value


def _check_path(value: str) -> str:
    """Refuse a path that is not a plain relative POSIX path."""
    if not value:
        raise ValueError("path must not be empty")
    if len(value) > MAX_PATH:
        raise ValueError(f"path must be at most {MAX_PATH} characters")
    if any(ch in value for ch in "\x00\r\n"):
        raise ValueError("path must not contain NUL, CR or LF")
    if value[0] in "/-:":
        raise ValueError("path must not start with '/', '-' or ':'")
    if ".." in value.split("/"):
        raise ValueError("path must not contain a '..' segment")
    return value


def _check_lines(start: int, end: int) -> None:
    """Refuse a range outside ``1 <= start <= end <= MAX_LINE``."""
    if not 1 <= start <= end <= MAX_LINE:
        raise ValueError(f"lines must satisfy 1 <= start <= end <= {MAX_LINE}")


Sha = Annotated[StrictStr, AfterValidator(_check_sha)]
"""A full lowercase hex commit SHA, never the all-zero one."""

RepoPath = Annotated[StrictStr, AfterValidator(_check_path)]
"""A relative POSIX path inside a repository, safe to put in a git argv."""


class _In(BaseModel):
    """Base of the request models: unknown fields refused.

    Every scalar field is ``Strict*`` (no ``"5"`` -> ``5``, no ``True`` ->
    ``1``); the model itself is not strict so nested models still accept
    the dicts a parsed JSON body holds.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class _Out(BaseModel):
    """Base of the response models: unknown fields ignored."""

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class OriginIn(_In):
    """A line range a commit owns, at that commit (a :class:`BlameOrigin`).

    Attributes
    ----------
    path : str
        File path at the owning commit, relative POSIX.
    start : int
        First line (1-based, inclusive).
    end : int
        Last line (1-based, inclusive).
    """

    path: RepoPath
    start: StrictInt
    end: StrictInt

    @model_validator(mode="after")
    def _range(self) -> OriginIn:
        _check_lines(self.start, self.end)
        return self


class HunkIn(_In):
    """One pushed, committed blame hunk.

    Attributes
    ----------
    sha : str
        The owning commit, a full lowercase hex SHA.
    origins : list[OriginIn]
        Its line ranges at ``sha`` (at most :data:`MAX_ORIGINS`).
    """

    sha: Sha
    origins: list[OriginIn] = Field(default_factory=list, max_length=MAX_ORIGINS)


class TargetIn(_In):
    """The code chunk a request is about (a ``mcp.targets.Target``).

    Attributes
    ----------
    path : str
        File path, relative POSIX.
    line_start : int
        First line (1-based, inclusive).
    line_end : int
        Last line (1-based, inclusive).
    qualified_name : str or None
        The symbol name, sent only when its declaration is pushed; at most
        :data:`MAX_QUALIFIED_NAME` printable characters.
    """

    path: RepoPath
    line_start: StrictInt
    line_end: StrictInt
    qualified_name: StrictStr | None = None

    @field_validator("qualified_name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value or len(value) > MAX_QUALIFIED_NAME:
            raise ValueError(
                f"qualified_name must be 1 to {MAX_QUALIFIED_NAME} characters"
            )
        if not value.isprintable():
            raise ValueError("qualified_name must be printable")
        return value

    @model_validator(mode="after")
    def _range(self) -> TargetIn:
        _check_lines(self.line_start, self.line_end)
        return self


class EvidenceIn(_In):
    """``POST /api/v1/projects/{slug}/evidence``.

    Attributes
    ----------
    target : TargetIn
        The code chunk.
    hunks : list[HunkIn]
        Its pushed, committed blame hunks (at most :data:`MAX_HUNKS`).
    limit : int
        Cap on the evidence items, ``1`` to :data:`MAX_LIMIT` (default 20).
    """

    target: TargetIn
    hunks: list[HunkIn] = Field(default_factory=list, max_length=MAX_HUNKS)
    limit: StrictInt = Field(default=20, ge=1, le=MAX_LIMIT)


class RationaleIn(_In):
    """``POST /api/v1/projects/{slug}/rationale``.

    Attributes
    ----------
    target : TargetIn
        The code chunk.
    hunks : list[HunkIn]
        Its pushed, committed blame hunks (at most :data:`MAX_HUNKS`).
    """

    target: TargetIn
    hunks: list[HunkIn] = Field(default_factory=list, max_length=MAX_HUNKS)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class CommitOut(_Out):
    """A scanned commit (``mcp.evidence._commit_dict``)."""

    sha: str
    llm_description: str | None = None
    subject: str
    body: str
    author_name: str
    author_email: str
    committed_at: str


class PullRequestOut(_Out):
    """A pull request (``mcp.evidence._pr_dict``)."""

    number: int
    title: str
    body: str | None = None
    state: str
    merged_at: str | None = None
    author: str | None = None
    html_url: str
    labels: list[Any] = Field(default_factory=list)
    commit_titles: list[Any] = Field(default_factory=list)
    comments: list[Any] = Field(default_factory=list)


class IssueOut(_Out):
    """An issue (``mcp.evidence._issue_dict``)."""

    number: int
    title: str
    body: str | None = None
    state: str
    author: str | None = None
    html_url: str
    labels: list[Any] = Field(default_factory=list)


class EvidenceOut(_Out):
    """One evidence item (``mcp.evidence._evidence_dict``).

    Attributes
    ----------
    commit : CommitOut
        The commit.
    pull_requests : list[PullRequestOut]
        The PRs containing it.
    issues : list[IssueOut]
        The issues those PRs close.
    source : str
        How the commit was found (:data:`Source`).
    push_status : str or None
        Set only by a connected local portal, for an item the platform
        could not answer (:data:`PushStatus`); omitted when ``None``.
    """

    commit: CommitOut
    pull_requests: list[PullRequestOut] = Field(default_factory=list)
    issues: list[IssueOut] = Field(default_factory=list)
    source: Source
    push_status: PushStatus | None = None

    @model_serializer(mode="wrap")
    def _drop_unset_push_status(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        data = handler(self)
        if data.get("push_status") is None:
            data.pop("push_status", None)
        return data


class TargetOut(_Out):
    """A resolved target (``mcp.targets.target_dict``).

    ``index_stale`` is serialized only when ``True``, as ``target_dict``
    adds it only then.
    """

    path: str
    line_start: int
    line_end: int
    qualified_name: str | None = None
    index_stale: bool = False

    @model_serializer(mode="wrap")
    def _drop_fresh_index(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        data = handler(self)
        if not data.get("index_stale"):
            data.pop("index_stale", None)
        return data


class EvidenceCountOut(_Out):
    """The ``evidence_count`` of a rationale card."""

    commits: int
    prs: int
    issues: int


class RationaleOut(_Out):
    """A rationale card (``mcp.rationale._format_response``)."""

    target: TargetOut
    purpose: str
    why: str
    constraints: list[str] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    model: str
    provider: str
    cached_at: str
    evidence_count: EvidenceCountOut


class StatusOut(_Out):
    """A platform project as a token sees it (``GET /api/v1/projects/{slug}``).

    Attributes
    ----------
    slug, name : str
        The project's slug and display name.
    github_full_name : str or None
        ``owner/name`` of its GitHub repository.
    clone_url : str or None
        The URL the platform clones from (its host is what a checkout's
        ``origin`` is matched against).
    default_branch : str or None
        The branch the platform scans.
    last_scanned_head : str or None
        The default-branch SHA of the last finished scan.
    last_scan_at : str or None
        When that scan finished (ISO 8601).
    access_lost : bool
        The platform can no longer reach the repository.
    role : str
        The token user's role (:data:`Role`).
    """

    slug: str
    name: str
    github_full_name: str | None = None
    clone_url: str | None = None
    default_branch: str | None = None
    last_scanned_head: str | None = None
    last_scan_at: str | None = None
    access_lost: bool = False
    role: Role


class ErrorOut(_Out):
    """The error envelope of every ``/api/v1`` route.

    Attributes
    ----------
    error : str
        Human-readable message (never git stderr).
    code : str
        Machine-readable code.
    reason : str or None
        Why a token was refused (its revocation reason), when it was.
    retry_after : int or None
        Seconds to wait, on a ``429`` / ``503``.
    """

    error: str
    code: str
    reason: str | None = None
    retry_after: int | None = None


class MetaOut(_Out):
    """``GET /api/v1/meta``: what a platform speaks.

    Attributes
    ----------
    api_version : int
        The contract version (:data:`API_VERSION` for this client).
    min_client : str
        The oldest WhyGraph release the platform still serves (semver).
    capabilities : list[str]
        Route families the platform offers (``evidence``, ``rationale``,
        ``history``, ``resources``).
    """

    api_version: int
    min_client: str
    capabilities: list[str] = Field(default_factory=list)


class TokenReply(_Out):
    """``POST /api/connect/token``: the code exchange's answer.

    Attributes
    ----------
    token : str
        The opaque ``wgc_`` connection token (shown once).
    org : str
        The org slug the project belongs to.
    project : StatusOut
        The linked project.
    api_version : int
        The contract version the platform speaks.
    """

    token: str
    org: str
    project: StatusOut
    api_version: int


class EvidenceReplyOut(_Out):
    """``POST /api/v1/projects/{slug}/evidence``: items plus unanswered SHAs."""

    evidence: list[EvidenceOut] = Field(default_factory=list)
    unknown_shas: list[str] = Field(default_factory=list)
    project: StatusOut | None = None


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------


def hunk_in_from_blame(hunk: BlameHunk) -> HunkIn:
    """The wire form of a committed blame hunk.

    Parameters
    ----------
    hunk : BlameHunk
        A hunk from :meth:`whygraph.services.git.Repository.blame`.

    Returns
    -------
    HunkIn
        Its SHA and origin ranges (``final_start`` stays local).

    Raises
    ------
    pydantic.ValidationError
        For an uncommitted hunk, or one past the size limits.
    """
    return HunkIn(
        sha=hunk.sha,
        origins=[OriginIn(path=o.path, start=o.start, end=o.end) for o in hunk.origins],
    )


def blame_hunks_from_in(hunks: Sequence[HunkIn]) -> tuple[BlameHunk, ...]:
    """The :class:`BlameHunk` values ``evidence_from_hunks`` takes, from the wire.

    The wire carries no metadata and no ``final_start``; each origin's
    ``final_start`` is its position in the request (1-based, in order),
    which keeps the request's order for the walk-past's ordering.

    Parameters
    ----------
    hunks : Sequence[HunkIn]
        Validated request hunks.

    Returns
    -------
    tuple[BlameHunk, ...]
        One hunk per entry, in order.
    """
    out: list[BlameHunk] = []
    position = 0
    for hunk in hunks:
        origins: list[BlameOrigin] = []
        for origin in hunk.origins:
            position += 1
            origins.append(BlameOrigin(origin.path, origin.start, origin.end, position))
        out.append(
            BlameHunk(
                sha=hunk.sha,
                lines_owned=sum(o.end - o.start + 1 for o in hunk.origins),
                author_name=None,
                author_email=None,
                summary=None,
                committed_at=None,
                origins=tuple(origins),
            )
        )
    return tuple(out)


__all__ = [
    "API_VERSION",
    "MAX_HUNKS",
    "MAX_LIMIT",
    "MAX_LINE",
    "MAX_ORIGINS",
    "MAX_PATH",
    "MAX_QUALIFIED_NAME",
    "CommitOut",
    "ErrorOut",
    "EvidenceCountOut",
    "EvidenceIn",
    "EvidenceOut",
    "EvidenceReplyOut",
    "HunkIn",
    "IssueOut",
    "MetaOut",
    "OriginIn",
    "PullRequestOut",
    "PushStatus",
    "RationaleIn",
    "RationaleOut",
    "RepoPath",
    "Role",
    "Sha",
    "Source",
    "StatusOut",
    "TargetIn",
    "TargetOut",
    "TokenReply",
    "blame_hunks_from_in",
    "hunk_in_from_blame",
]
