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
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_serializer,
    model_validator,
)

from whygraph.core.config import DEPTH_LEVELS, PLANS_DIR_RE
from whygraph.services.git import BlameHunk, BlameOrigin

API_VERSION = 2
"""The ``/api/v1`` contract version.

2 (plan 54): each card ``constraints`` / ``risks`` item is an object
(:class:`RationaleItemOut`), not a string. Both sides compare it at
``/meta``, so a client of another version reports ``update_required``
rather than failing to parse a card.
"""

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

PLATFORM_BUDGET_MESSAGE = (
    "the platform's monthly budget for this project, organization or your "
    "account is reached"
)
"""The ``error`` of ``403 budget_exceeded`` (M2f-2): scope-neutral, because a
connected portal shows the platform's message as it is; ``scope`` says which."""

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

Source = Literal["blame", "pr-origin", "blame-walked", "predecessor-blame", "area"]
"""An evidence item's ``source`` label (see ``mcp.evidence._SOURCE_PRIORITY``)."""

PushStatus = Literal[
    "uncommitted", "not_pushed", "not_on_default_branch", "pending_scan"
]
"""Why a connected portal could not get evidence for a SHA from the platform."""

Role = Literal["owner", "admin", "member"]
"""The token user's role in the project's org (never ``reader``)."""

ProjectRoleName = Literal["admin", "contributor", "viewer"]
"""The token user's effective role on the project (M2f-1)."""

Basis = Literal["quoted", "cited", "inferred", "unknown"]
"""How a card's constraint or risk is sourced (``analyze.ITEM_BASES``)."""

RefKind = Literal["commit", "pr", "issue"]
"""What a constraint's or risk's ref points at (``analyze.REF_KINDS``)."""

Intent = Literal["explain", "edit", "refactor", "delete", "review", "plan"]
"""The task a card is for (``mcp.rationale_depth.INTENTS``, plan 62 section 14.3)."""

DepthLevel = Literal[DEPTH_LEVELS]  # type: ignore[valid-type]
"""A depth a request may ask for: ``low`` or ``medium`` (R49, R51). Answers carry a level as a plain string, so a client
never refuses a card of a level it does not know yet."""

RATIONALE_DEPTH_CAPABILITY = "rationale_depth"
"""The ``meta`` capability of a platform whose ``/rationale`` takes ``intent``,
``depth`` and ``cached_only`` (plan 62 section 14.4)."""


PLAN_STEM_PATTERN = r"[a-z0-9][a-z0-9-]{0,79}"
"""A plan stem (``mcp.plans.PLAN_STEM_RE``), for a route path and a wire field."""

MAX_PLAN_PATHS = 20
"""Most paths one ``plans/for`` request may carry."""

MAX_PLAN_SECTION = 299
"""Highest plan section number a route serves."""

PLAN_TITLE_MAX = 80
"""Longest plan or hint title on the wire, in characters."""

PLAN_SECTION_TEXT_MAX = 8000
"""Longest section text on the wire, in characters."""


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
    intent : str or None
        The task the card is for (:data:`Intent`); the platform resolves
        the depth from it under its ``auto`` setting.
    depth : str or None
        An explicit level (:data:`DepthLevel`), capped at the platform
        project's ceiling.
    cached_only : bool or None
        Never generate; ``None`` means ``True`` for ``intent="review"`` and
        ``False`` otherwise.

    Notes
    -----
    The three are sent only to a platform whose ``meta`` lists
    :data:`RATIONALE_DEPTH_CAPABILITY` (an older one refuses an unknown
    field), and a ``None`` one is left out of the body.
    """

    target: TargetIn
    hunks: list[HunkIn] = Field(default_factory=list, max_length=MAX_HUNKS)
    intent: Intent | None = None
    depth: DepthLevel | None = None
    cached_only: StrictBool | None = None

    @model_serializer(mode="wrap")
    def _drop_unset_depth_inputs(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        data = handler(self)
        for key in ("intent", "depth", "cached_only"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class PlansTargetIn(_In):
    """The symbol a ``plans/for`` request is about (scope ``symbol``).

    The path travels in ``paths``; this carries only what the same call
    already sent (:class:`TargetIn` minus the path).

    Attributes
    ----------
    qualified_name : str or None
        The symbol name; when the platform's index resolves it, the
        platform's own range is the anchor.
    line_start, line_end : int or None
        The range in the sender's working tree (both or neither).
    """

    qualified_name: StrictStr | None = None
    line_start: StrictInt | None = None
    line_end: StrictInt | None = None

    @field_validator("qualified_name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value or len(value) > MAX_QUALIFIED_NAME or not value.isprintable():
            raise ValueError(
                f"qualified_name must be 1 to {MAX_QUALIFIED_NAME} printable characters"
            )
        return value

    @model_validator(mode="after")
    def _range(self) -> PlansTargetIn:
        if (self.line_start is None) != (self.line_end is None):
            raise ValueError("line_start and line_end go together")
        if self.line_start is not None and self.line_end is not None:
            _check_lines(self.line_start, self.line_end)
        return self


class PlansForIn(_In):
    """``POST /api/v1/projects/{slug}/plans/for`` (plan 61 section 4.7).

    Attributes
    ----------
    shas : list[str]
        The commit SHAs of the sender's answer (at most :data:`MAX_LIMIT`).
    paths : list[str]
        Its file path(s) (at most :data:`MAX_PLAN_PATHS`).
    scope : {"symbol", "file"}
        Which relevance rules apply (default ``file``).
    detail : {"compact", "full"}
        ``full`` adds ``path``, ``present`` and ``commits`` to each ref.
    target : PlansTargetIn or None
        The symbol, for scope ``symbol``; with none only ``commit`` and
        ``pr`` links apply.
    """

    shas: list[Sha] = Field(default_factory=list, max_length=MAX_LIMIT)
    paths: list[RepoPath] = Field(default_factory=list, max_length=MAX_PLAN_PATHS)
    scope: Literal["symbol", "file"] = "file"
    detail: Literal["compact", "full"] = "compact"
    target: PlansTargetIn | None = None


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
    """A pull request (``mcp.evidence._pr_dict``).

    The review fields (plan 62) default to empty, so an older platform's
    answer still parses; the platform always sends them, the totals never
    ``None``. ``review_threads`` holds only the threads on the request's
    file (and its rename aliases), each with ``current``.
    """

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
    review_summaries: list[Any] = Field(default_factory=list)
    review_threads: list[Any] = Field(default_factory=list)
    review_threads_elsewhere: int = 0
    review_threads_total: int = 0


class IssueOut(_Out):
    """An issue (``mcp.evidence._issue_dict``).

    ``comments`` / ``comments_total`` (plan 62) default to empty, so an
    older platform's answer still parses.
    """

    number: int
    title: str
    body: str | None = None
    state: str
    author: str | None = None
    html_url: str
    labels: list[Any] = Field(default_factory=list)
    comments: list[Any] = Field(default_factory=list)
    comments_total: int = 0


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


class RationaleItemRefOut(_Out):
    """A source one constraint or risk names (``analyze.ItemRef.as_dict``).

    Attributes
    ----------
    kind : str
        ``"commit"``, ``"pr"`` or ``"issue"`` (:data:`RefKind`).
    id : str
        The commit's full SHA, or the pull request's or issue's number.
    url : str or None
        The pull request's or issue's ``html_url``; omitted when ``None``.
    """

    kind: RefKind
    id: str
    url: str | None = None

    @model_serializer(mode="wrap")
    def _drop_unset_url(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        if not data.get("url"):
            data.pop("url", None)
        return data


class RationaleItemOut(_Out):
    """One constraint or risk of a card, with its source (``analyze.Item.as_dict``).

    Attributes
    ----------
    text : str
        The constraint or the risk.
    basis : str
        How its source was checked (:data:`Basis`).
    refs : list[RationaleItemRefOut]
        The named sources found in the evidence.
    quote : str
        The checked quote; omitted when empty.
    """

    text: str
    basis: Basis
    refs: list[RationaleItemRefOut] = Field(default_factory=list)
    quote: str = ""

    @model_serializer(mode="wrap")
    def _drop_empty_quote(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        data = handler(self)
        if not data.get("quote"):
            data.pop("quote", None)
        return data


class RationaleDepthOut(_Out):
    """A card's ``depth`` (``mcp.rationale.RationaleDepth``).

    ``requested`` is ``"auto"`` or a level, ``resolved`` the card's level;
    both are plain strings, so a level a client does not know yet parses.
    """

    requested: str
    resolved: str
    reason: str = ""


class RationaleDeeperOut(_Out):
    """A card's ``deeper``: the next level and what it would add (``mcp.rationale.RationaleDeeper``)."""

    level: str
    adds: str = ""


class RationaleCoverageOut(_Out):
    """A card's ``coverage``: what its prompt left out (``analyze.CardCoverage.as_dict``).

    ``source`` is omitted when ``None``; a key a newer platform adds is
    ignored.
    """

    full_prs_demoted: int = 0
    threads_lost: int = 0
    trimmed: list[str] = Field(default_factory=list)
    descriptions_missing: int = 0
    line_commits: int = 0
    context_commits: int = 0
    context_omitted: int = 0
    source: str | None = None

    @model_serializer(mode="wrap")
    def _drop_unset(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        if data.get("source") is None:
            data.pop("source", None)
        return data


NOT_CACHED = "not_cached"
"""The ``status`` of a ``cached_only`` miss: ``{"status", "target"}`` only."""


class RationaleOut(_Out):
    """A rationale card (``mcp.rationale._format_response``), or a ``cached_only`` miss.

    A card carries every required field; a miss (``status`` is
    :data:`NOT_CACHED`, only for a request with ``cached_only``) carries
    ``target`` alone. ``depth``, ``coverage`` and ``deeper`` (plan 62
    section 14.4) are ``None`` from a platform that predates them, and a
    ``None`` one is left out when serialized (:data:`_OPTIONAL_CARD_KEYS`),
    as a connected portal leaves out a ``None`` ``partial``. ``match``,
    ``stale`` and ``card_range`` come with a ``cached_only`` card.
    """

    target: TargetOut
    status: str | None = None
    purpose: str | None = None
    why: str | None = None
    constraints: list[RationaleItemOut] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    risks: list[RationaleItemOut] = Field(default_factory=list)
    model: str | None = None
    provider: str | None = None
    cached_at: str | None = None
    evidence_count: EvidenceCountOut | None = None
    # None from a platform that predates them, so it never reports a card
    # it built under the old 5-commit bound as final.
    partial: bool | None = None
    describing: int | None = None
    completing: bool | None = None
    depth: RationaleDepthOut | None = None
    coverage: RationaleCoverageOut | None = None
    deeper: RationaleDeeperOut | None = None
    match: str | None = None
    stale: bool | None = None
    card_range: dict[str, int] | None = None

    @model_validator(mode="after")
    def _a_card_or_a_miss(self) -> RationaleOut:
        if self.status == NOT_CACHED:
            return self
        missing = [
            name
            for name in (
                "purpose",
                "why",
                "model",
                "provider",
                "cached_at",
                "evidence_count",
            )
            if getattr(self, name) is None
        ]
        if missing:
            raise ValueError(f"a card needs {', '.join(missing)}")
        return self

    @model_serializer(mode="wrap")
    def _drop_unset(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        for key in _OPTIONAL_CARD_KEYS:
            if data.get(key) is None:
                data.pop(key, None)
        return data


_OPTIONAL_CARD_KEYS = (
    "status",
    "depth",
    "coverage",
    "deeper",
    "match",
    "stale",
    "card_range",
)
""":class:`RationaleOut` keys left out of its dump when ``None``.

``partial`` / ``describing`` / ``completing`` are not among them: the
connected portal drops those itself (``LinkedProject.rationale``).
"""


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
        The token user's role in the org (:data:`Role`).
    project_role : str
        The token user's effective role on the project
        (:data:`ProjectRoleName`): a ``viewer`` causes no LLM spend, so
        has no chat.
    plans_dir : str or None
        The project's ``[plans].dir`` (``""`` when plan indexing is off;
        ``None`` from a platform that does not send it), so a linked
        checkout's ``.whygraph/portal.env`` can name the folder to publish
        into. A value a one-line ``portal.env`` entry cannot hold is
        refused (plan 61 section 4.16).
    rationale_depth : str or None
        The project's ``[rationale].depth`` (``"auto"`` or a level;
        ``None`` from a platform that predates depth levels), shown
        read-only on a linked project (plan 62 section 14.5).
    rationale_max_depth : str or None
        The **effective** ceiling: the lower of the project's ``max_depth``
        and its org's ``org_max_depth``.
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
    project_role: ProjectRoleName
    plans_dir: str | None = Field(
        default=None, pattern=f"^(?:{PLANS_DIR_RE.pattern})?$"
    )
    rationale_depth: str | None = Field(default=None, max_length=16)
    rationale_max_depth: str | None = Field(default=None, max_length=16)


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
    scope : str or None
        Which budget refused a ``403 budget_exceeded``: ``"org"``,
        ``"project"`` or ``"member"`` (additive, M2f-2).
    """

    error: str
    code: str
    reason: str | None = None
    retry_after: int | None = None
    scope: str | None = None


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
    base_origin : str or None
        The platform's own (base) origin, the same on its base host and
        every org host, so a client given an org's address can find the
        platform it belongs to. ``None`` from a platform that predates it.
    """

    api_version: int
    min_client: str
    capabilities: list[str] = Field(default_factory=list)
    base_origin: str | None = Field(default=None, max_length=2048)


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
    api_origin : str or None
        Not sent by this platform. A client computes the org origin itself
        from the platform origin and ``org`` and refuses a reply that names
        another one (M2e plan section 4.7), so the field is read only to be
        checked, never trusted.
    """

    token: str
    org: str
    project: StatusOut
    api_version: int
    api_origin: str | None = None


class EvidenceReplyOut(_Out):
    """``POST /api/v1/projects/{slug}/evidence``: items plus unanswered SHAs."""

    evidence: list[EvidenceOut] = Field(default_factory=list)
    unknown_shas: list[str] = Field(default_factory=list)
    project: StatusOut | None = None


class PlanHintOut(_Out):
    """One hint beside a card: a plan step or decision row that names the symbol."""

    id: str
    title: str | None = None
    n: int


class PlanRefOut(_Out):
    """A pointer to a plan (``mcp.plans.plans_for``; no ``objective``, R22).

    The platform always sends the fields of the scope and detail asked for;
    a linked portal trims to compact.
    """

    stem: str
    title: str = Field(max_length=PLAN_TITLE_MAX)
    status: str
    link: str
    uri: str
    hints: list[PlanHintOut] | None = Field(default=None, max_length=2)
    sections: list[int] | None = None
    sections_total: int | None = None
    path: str | None = None
    present: bool | None = None
    commits: list[str] | None = None


class PlansForOut(_Out):
    """``POST /api/v1/projects/{slug}/plans/for``."""

    plans: list[PlanRefOut] = Field(default_factory=list)
    project: StatusOut | None = None


class PlanCommitOut(_Out):
    """An implementing commit in a plan record (8-character SHA)."""

    sha: str
    subject: str | None = None
    on_default_branch: bool | None = None


class PlanPullRequestOut(_Out):
    """An implementing pull request in a plan record."""

    number: int
    title: str | None = None
    state: str | None = None


class PlanContentsOut(_Out):
    """One level-2 entry of a plan's contents."""

    n: int
    title: str
    chars: int = 0
    children: int = 0


class PlanOut(_Out):
    """``GET /api/v1/projects/{slug}/plans/{stem}``: digest, contents, links.

    There is no body field: no route returns a whole plan (R15).
    """

    kind: str = "plan"
    reading: str | None = None
    stem: str
    title: str = Field(max_length=PLAN_TITLE_MAX)
    status: str
    uri: str | None = None
    path: str | None = None
    present: bool | None = None
    objective: str | None = None
    line_count: int | None = None
    redacted_lines: int | None = None
    publishing_links: int | None = None
    implementing_commits: list[PlanCommitOut] = Field(default_factory=list)
    implementing_commits_total: int | None = None
    pull_requests: list[PlanPullRequestOut] = Field(default_factory=list)
    pull_requests_total: int | None = None
    paths: list[str] = Field(default_factory=list)
    paths_total: int | None = None
    digest: str = ""
    contents: list[PlanContentsOut] = Field(default_factory=list)
    contents_total: int | None = None
    project: StatusOut | None = None


class PlanChildOut(_Out):
    """A child entry of a plan section."""

    n: int
    title: str
    chars: int = 0


class PlanSectionOut(_Out):
    """``GET /api/v1/projects/{slug}/plans/{stem}/sections/{n}``.

    ``text`` is at most :data:`PLAN_SECTION_TEXT_MAX` characters, so the
    model itself refuses an over-long section.
    """

    kind: str = "plan"
    reading: str | None = None
    stem: str
    status: str
    n: int
    title: str
    level: int | None = None
    text: str = Field(max_length=PLAN_SECTION_TEXT_MAX)
    chars: int | None = None
    children: list[PlanChildOut] = Field(default_factory=list)
    children_total: int | None = None
    prev: int | None = None
    next: int | None = None
    version: str | None = None
    copies: bool | None = None
    copies_note: str | None = None
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
    "MAX_PLAN_PATHS",
    "MAX_PLAN_SECTION",
    "MAX_QUALIFIED_NAME",
    "NOT_CACHED",
    "PLAN_SECTION_TEXT_MAX",
    "PLAN_STEM_PATTERN",
    "PLAN_TITLE_MAX",
    "PLATFORM_BUDGET_MESSAGE",
    "RATIONALE_DEPTH_CAPABILITY",
    "Basis",
    "CommitOut",
    "DepthLevel",
    "ErrorOut",
    "EvidenceCountOut",
    "EvidenceIn",
    "EvidenceOut",
    "EvidenceReplyOut",
    "HunkIn",
    "Intent",
    "IssueOut",
    "MetaOut",
    "OriginIn",
    "PlanChildOut",
    "PlanCommitOut",
    "PlanContentsOut",
    "PlanHintOut",
    "PlanOut",
    "PlanPullRequestOut",
    "PlanRefOut",
    "PlanSectionOut",
    "PlansForIn",
    "PlansForOut",
    "PlansTargetIn",
    "ProjectRoleName",
    "PullRequestOut",
    "PushStatus",
    "RationaleCoverageOut",
    "RationaleDeeperOut",
    "RationaleDepthOut",
    "RationaleIn",
    "RationaleItemOut",
    "RationaleItemRefOut",
    "RationaleOut",
    "RefKind",
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
