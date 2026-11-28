"""Project notes on a checkout linked to a platform (plan 63 section 4.5).

A linked project keeps no WhyGraph database, so a note proposal is
validated here and forwarded to the platform's ``POST /notes``; the
platform stores it in its review queue. This module is the privacy line:
**no unpushed path, symbol name, SHA or file content crosses the link
through a structured field.** Free text (the body, a check's text and glob,
an area name) is sent verbatim, which the docs say plainly, but text shaped
like a GitHub token, a connection token or a provider key is refused
here (and again on the platform).

The anchor is narrowed by a ladder against ``D``, the default branch's
remote-tracking ref as the last fetch left it (never a fetch):

=================  =======================================================  ==========================
Asked              Sent when                                                Otherwise
=================  =======================================================  ==========================
project, area      always                                                   -
path ``P``         ``D`` tracks ``P``                                       project
range ``P:a-b``    the working-tree ``P`` is identical to ``D:P``           path ``P`` if ``D`` tracks
                                                                            it, else project
symbol ``Q``       the index is fresh for ``Q``'s file ``P`` **and** ``P``  path ``P`` if ``D`` tracks
                   is identical to ``D:P``                                  it, else project
=================  =======================================================  ==========================

A byte-identical file means every name and line in it exists on ``D``, so
no blame is needed; this closes the stale-index, renamed-scope and
feature-branch holes at once. Without ``D`` every anchor but project and
area becomes project. A ref is sent only as ``#N`` or as the full SHA of a
commit ``D`` contains; the rest are dropped with ``not_pushed``.

Every validator reused from :mod:`whygraph.mcp.notes` is free of the
project database, which a linked context refuses to open by construction.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from whygraph.core.remote import RemoteError, RemoteProject, platform_block
from whygraph.core.safe_paths import check_inside
from whygraph.services.git import Repository

from . import targets
from .errors import WhyGraphError
from .evidence import default_remote_refs, path_tracked_at
from .notes import (
    ANCHOR_KINDS,
    MAX_REFS,
    ProposeNoteResult,
    _check_kind,
    _clean_body,
    _clean_check,
    _invalid,
    _is_int,
    _ISSUE_RE,
    _mcp_anchor,
    _not_found,
    _open_graph,
    _range_fields,
    _resolve_symbol_for_write,
    _SHA_RE,
    _validate_path,
    refuse_secret_text,
)
from .targets import Target

_log = logging.getLogger(__name__)

NOT_PUSHED = "not_pushed"
"""Why the ladder or the ref filter withheld something: it is not on ``D``."""

NOT_ON_PLATFORM = "not_on_platform"
"""Why the platform withheld something: its clone lacks it (it retried)."""

AREA_MAX_CHARS = 128
"""The longest area name the wire contract takes (``NoteAnchorIn.area``)."""

_PASSED_CODES = frozenset(
    {
        "note_check_invalid",
        "note_invalid",
        "note_limit",
        "note_needs_second_confirmer",
        "note_no_actor",
        "note_not_found",
        "note_state",
        "throttled",
    }
)
"""Platform refusal codes a proposal passes through to the agent unchanged.

The ``note_*`` codes the platform's notes routes answer with, plus the
connection throttle. Any other reason (including a ``note_*`` code this
version does not know) becomes a plain error, so a platform can never put
an arbitrary code into a tool result.
"""


def linked_propose(
    remote: RemoteProject,
    body: object,
    kind: object,
    *,
    qualified_name: object = None,
    path: object = None,
    line_start: object = None,
    line_end: object = None,
    area: object = None,
    project_wide: object = None,
    refs: object = None,
    check: object = None,
    supersedes_id: object = None,
) -> ProposeNoteResult:
    """``whygraph_propose_note`` for a linked project: validate, narrow, forward.

    Parameters
    ----------
    remote : RemoteProject
        The project's platform.
    body, kind : object
        The note's text and kind (``decision`` / ``constraint`` / ``gotcha``
        / ``convention``).
    qualified_name, path, line_start, line_end, area, project_wide : object
        The anchor, exactly one mode, as the MCP tool takes it.
    refs : object
        ``None`` or a list of at most five commit SHAs (a prefix is fine)
        or ``#N`` references.
    check : object
        ``None`` or ``{"kind", "text", "glob"}`` (a constraint's check).
    supersedes_id : object
        ``None`` or the confirmed note a revision replaces.

    Returns
    -------
    ProposeNoteResult
        The platform's answer (``note_id``, ``status``, ``duplicate``,
        ``review`` - a full platform URL - and ``note`` when it may be
        shown), plus ``anchor_downgraded`` when the anchor was narrowed,
        ``dropped_refs`` when a ref was withheld, and ``platform``.

    Raises
    ------
    WhyGraphError
        ``note_invalid`` / ``note_check_invalid`` from local validation
        (no request is made), including a body, check or area that holds
        token-shaped text; ``note_unsupported`` for a platform that cannot
        take notes; the platform's ``note_*`` or ``throttled`` code; a
        plain error for an unreachable or revoked link.
    """
    # 1. Local validation: any failure here makes no request at all.
    text = _clean_body(body)
    note_kind = _check_kind(kind)
    columns = _clean_check(note_kind, check)
    if supersedes_id is not None:
        if not _is_int(supersedes_id):
            raise _invalid("supersedes_id must be a note id")
        if supersedes_id < 1:  # type: ignore[operator]
            raise _not_found(supersedes_id)
    if qualified_name is not None and not isinstance(qualified_name, str):
        raise _invalid("qualified_name must be text")
    name = qualified_name.strip() if isinstance(qualified_name, str) else None
    asked = _mcp_anchor(name, path, line_start, line_end, area, project_wide)  # type: ignore[arg-type]
    if asked["kind"] == "area" and (
        not isinstance(area, str)
        or not area.strip()
        or len(area.strip()) > AREA_MAX_CHARS
    ):
        raise _invalid("unknown area: anchor to a configured area")
    refuse_secret_text(text, columns.get("check_text"), columns.get("check_glob"), area)
    ref_items = _parse_refs(refs)
    local = _local_anchor(asked)

    # 2. The ladder.
    repo = Repository(targets.repo_root())
    default = default_remote_refs(repo)[:1]
    sent, local_reason = _ladder(repo, default, asked, local)

    # 3. Refs: only #N and full SHAs D contains.
    kept, dropped = _push_safe_refs(repo, default, ref_items)

    # 4. The request.
    payload: dict[str, Any] = {
        "body": text,
        "kind": note_kind,
        "anchor": sent,
        "fallback_to_project": True,
        "refs": kept,
    }
    if columns.get("check_kind") is not None:
        payload["check"] = {
            "kind": columns["check_kind"],
            "text": columns["check_text"],
            "glob": columns["check_glob"],
        }
    if supersedes_id is not None:
        payload["supersedes_id"] = supersedes_id
    try:
        reply = remote.propose_note(payload)
    except RemoteError as exc:
        if exc.reason == "note_unsupported":
            raise WhyGraphError(
                "the platform this project is linked to cannot take notes yet - "
                "ask its admin to update WhyGraph",
                code="note_unsupported",
            ) from exc
        if exc.status == "ok" and exc.reason in _PASSED_CODES:
            raise WhyGraphError(str(exc), code=exc.reason) from exc
        raise WhyGraphError(str(exc)) from exc

    # 5. The merged result.
    return _result(reply, asked["kind"], sent["kind"], local_reason, dropped, remote)


def linked_notes(
    remote: RemoteProject,
    repo: Repository,
    target: Target,
    sendable: Target | None,
) -> tuple[list[dict] | None, bool]:
    """The platform's notes for a rationale card's target; never raises.

    Only what the card's own request already sent (``sendable``, from
    :func:`whygraph.mcp.evidence.pushed_target`) or a path ``D`` tracks is
    asked about.

    Parameters
    ----------
    remote : RemoteProject
        The project's platform.
    repo : Repository
        The checkout.
    target : Target
        The locally resolved target.
    sendable : Target or None
        What the card request sent, or ``None`` (the ``no_evidence`` path).

    Returns
    -------
    tuple[list[dict] or None, bool]
        The notes and whether the platform serves notes. ``(None, True)``
        when nothing may be asked about or the call failed (logged);
        ``([], False)`` for a platform without notes.

    Notes
    -----
    * ``sendable`` with the target's own path: path, name and lines.
    * ``sendable`` with a substituted (pushed origin) path: that path only.
    * no ``sendable``: the target's path only, when ``D`` tracks it.
    """
    try:
        if sendable is not None and sendable.path == target.path:
            notes, supported = remote.notes_for(
                sendable.path,
                qualified_name=sendable.qualified_name,
                lines=(sendable.line_start, sendable.line_end),
            )
        elif sendable is not None:
            notes, supported = remote.notes_for(sendable.path)
        elif path_tracked_at(repo, target.path, default_remote_refs(repo)[:1]):
            notes, supported = remote.notes_for(target.path)
        else:
            return None, True
    except Exception as exc:  # noqa: BLE001 - a card never fails over its notes
        _log.warning("platform notes lookup failed (%s): %s", type(exc).__name__, exc)
        return None, True
    return notes, supported


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _parse_refs(refs: object) -> list[tuple[str, str]]:
    """Check the refs' shape before any git call: ``[(kind, text)]``.

    ``kind`` is ``issue`` (``#N``, normalised) or ``sha`` (a 7 to 40 hex
    prefix, lower case). Anything else is refused, as the local tool does.
    """
    if refs is None:
        return []
    if isinstance(refs, str | bytes) or not isinstance(refs, Iterable):
        raise _invalid("refs must be a list")
    items = list(refs)
    if len(items) > MAX_REFS:
        raise _invalid(f"refs must hold at most {MAX_REFS} entries")
    out: list[tuple[str, str]] = []
    for index, ref in enumerate(items):
        if not isinstance(ref, str):
            raise _invalid(f"refs[{index}] must be a commit SHA or #N")
        text = ref.strip()
        issue = _ISSUE_RE.fullmatch(text)
        if issue:
            out.append(("issue", f"#{int(issue.group(1))}"))
        elif _SHA_RE.fullmatch(text):
            out.append(("sha", text.lower()))
        else:
            raise _invalid(f"refs[{index}] must be a commit SHA or #N")
    return out


def _local_anchor(asked: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the anchor against the checkout; return what the ladder needs.

    Path and range anchors carry the **normalised** path
    (:func:`~whygraph.mcp.notes._validate_path`: deny-list, symlinks,
    existence, exact case); a range is also checked against the file
    (:func:`~whygraph.mcp.notes._range_fields`); a symbol is resolved in
    the local index into its :class:`Target`.
    """
    kind = asked["kind"]
    if kind == "path":
        return {"path": _validate_path(asked["path"])}
    if kind == "range":
        rel = _validate_path(asked["path"])
        fields = _range_fields(rel, asked["line_start"], asked["line_end"])
        return {
            "path": fields["anchor_path"],
            "line_start": fields["line_start"],
            "line_end": fields["line_end"],
        }
    if kind == "symbol":
        name = asked["qualified_name"]
        if not isinstance(name, str) or not name:
            raise _invalid("a symbol anchor needs a qualified name")
        return {"qualified_name": name, "target": _resolve_symbol_for_write(name)}
    if kind == "area":
        return {"area": str(asked["area"]).strip()}
    return {}


# ---------------------------------------------------------------------------
# The ladder (section 4.5 step 2)
# ---------------------------------------------------------------------------


def _ladder(
    repo: Repository,
    default: tuple[str, ...],
    asked: Mapping[str, Any],
    local: Mapping[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """The anchor to send and ``not_pushed`` when it is narrower than asked."""
    kind = asked["kind"]
    if kind == "project":
        return {"kind": "project"}, None
    if kind == "area":
        return {"kind": "area", "area": local["area"]}, None
    project = {"kind": "project"}
    if not default:
        return project, NOT_PUSHED
    ref = default[0]
    if kind == "path":
        if path_tracked_at(repo, local["path"], default):
            return {"kind": "path", "path": local["path"]}, None
        return project, NOT_PUSHED
    if kind == "range":
        rel = local["path"]
        if repo.worktree_matches(ref, rel):
            return {
                "kind": "range",
                "path": rel,
                "line_start": local["line_start"],
                "line_end": local["line_end"],
            }, None
        return _path_or_project(repo, default, rel), NOT_PUSHED
    # symbol
    target: Target = local["target"]
    if (
        not target.index_stale
        and _index_fresh(target.path)
        and repo.worktree_matches(ref, target.path)
    ):
        return {"kind": "symbol", "qualified_name": local["qualified_name"]}, None
    try:
        # The index's path, held to the same rules as a path the caller gave.
        rel = _validate_path(target.path)
    except WhyGraphError:
        return project, NOT_PUSHED
    return _path_or_project(repo, default, rel), NOT_PUSHED


def _path_or_project(
    repo: Repository, default: tuple[str, ...], rel: str
) -> dict[str, Any]:
    """A path anchor when ``D`` tracks ``rel``, else a project anchor."""
    if path_tracked_at(repo, rel, default):
        return {"kind": "path", "path": rel}
    return {"kind": "project"}


def _index_fresh(rel: str) -> bool:
    """Whether CodeGraph recorded ``rel``'s current bytes (sha256) when it indexed it.

    Stricter than :attr:`Target.index_stale`, which reads an index without a
    recorded hash as fresh: here an unknown hash, an unreadable file or an
    unopenable index all answer ``False``, so the symbol is not sent.
    """
    root = targets.repo_root()
    try:
        with _open_graph(root) as graph:
            recorded = graph.file_hash(rel)
        if recorded is None:
            return False
        current = hashlib.sha256(check_inside(root, rel).read_bytes()).hexdigest()
    except Exception:  # noqa: BLE001 - any doubt withholds the name
        return False
    return current == recorded


def _push_safe_refs(
    repo: Repository, default: tuple[str, ...], items: list[tuple[str, str]]
) -> tuple[list[str], list[dict[str, str]]]:
    """``#N`` refs and full SHAs ``D`` contains; the rest dropped as ``not_pushed``."""
    kept: list[str] = []
    dropped: list[dict[str, str]] = []
    for kind, text in items:
        if kind == "issue":
            value: str | None = text
        else:
            sha = repo.resolve_commit(text) if default else None
            pushed = sha is not None and repo.is_ancestor(sha, default[0]) is True
            value = sha if pushed else None
        if value is None:
            if not any(d["ref"] == text for d in dropped):
                dropped.append({"ref": text, "reason": NOT_PUSHED})
        elif value not in kept:
            kept.append(value)
    return kept, dropped


# ---------------------------------------------------------------------------
# The result (section 4.5 step 5)
# ---------------------------------------------------------------------------


def _result(
    reply: Mapping[str, Any],
    asked_kind: str,
    sent_kind: str,
    local_reason: str | None,
    local_dropped: list[dict[str, str]],
    remote: RemoteProject,
) -> ProposeNoteResult:
    """Merge the platform's answer with what the ladder and ref filter did."""
    result: ProposeNoteResult = {
        "note_id": int(reply["note_id"]),
        "status": str(reply["status"]),
        "duplicate": bool(reply.get("duplicate", False)),
        "review": str(reply["review"]),
    }
    if reply.get("note") is not None:
        result["note"] = reply["note"]
    final_kind = sent_kind
    platform_moved = reply.get("anchor_downgraded")
    if isinstance(platform_moved, Mapping):
        to = platform_moved.get("to")
        final_kind = to if isinstance(to, str) and to in ANCHOR_KINDS else "project"
    if local_reason is not None or isinstance(platform_moved, Mapping):
        result["anchor_downgraded"] = {
            "from": asked_kind,
            "to": final_kind,
            "reason": NOT_PUSHED if local_reason is not None else NOT_ON_PLATFORM,
        }
    dropped: list[dict[str, str]] = list(local_dropped)
    for item in reply.get("dropped_refs") or ():
        if isinstance(item, Mapping) and isinstance(item.get("ref"), str):
            dropped.append({"ref": item["ref"], "reason": NOT_ON_PLATFORM})
    if dropped:
        result["dropped_refs"] = dropped  # type: ignore[typeddict-item]
    result["platform"] = platform_block(remote)
    return result


__all__ = ["NOT_ON_PLATFORM", "NOT_PUSHED", "linked_notes", "linked_propose"]
