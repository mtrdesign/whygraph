"""The security event log.

One structured ``INFO`` record per security-relevant event on the
``whygraph.portal.audit`` logger. Callers pass identifiers only: never a
token, secret or password.

A production portal also persists every event (M2f-1 plan section 4.9):
its lifespan registers an :class:`~whygraph.portal.audit_store.AuditWriter`
(:func:`set_writer`), and :func:`audit` hands it a copy of each record -
never blocking and never failing the request. With no writer (local mode,
a test without a lifespan) the log line is all there is.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .audit_store import AuditWriter

logger = logging.getLogger("whygraph.portal.audit")

_writer: AuditWriter | None = None
_writer_lock = threading.Lock()


def set_writer(writer: AuditWriter | None) -> None:
    """Register the writer that persists events, or ``None`` to stop persisting.

    Parameters
    ----------
    writer : AuditWriter or None
        The started writer; ``None`` unregisters the current one.
    """
    global _writer
    with _writer_lock:
        _writer = writer


def clear_writer(writer: AuditWriter) -> None:
    """Unregister ``writer`` - only when it is still the registered one.

    Parameters
    ----------
    writer : AuditWriter
        The writer a lifespan registered and is about to stop.
    """
    global _writer
    with _writer_lock:
        if _writer is writer:
            _writer = None


def current_writer() -> AuditWriter | None:
    """The registered writer, if any (tests flush it before reading the table)."""
    return _writer


def truncate_email(email: str | None) -> str:
    """Shorten an email for the log: 3 characters of the local part + domain.

    Parameters
    ----------
    email : str or None
        An address, possibly malformed.

    Returns
    -------
    str
        ``"ali...@example.com"`` style; ``""`` for no input.
    """
    if not email:
        return ""
    local, sep, domain = email.strip().partition("@")
    return f"{local[:3]}...{sep}{domain}"


def audit(
    event: str,
    request: Any,
    *,
    uid: str | None = None,
    target: str | None = None,
    org_id: int | None = None,
    org: str | None = None,
    **fields: Any,
) -> None:
    """Write one security event record.

    Parameters
    ----------
    event : str
        Event name, e.g. ``login_failure``.
    request : starlette.requests.Request or mapping
        The request (or its ASGI scope), for the client address and host.
    uid : str, optional
        The acting user's uid.
    target : str, optional
        The affected user's uid when it differs from ``uid``.
    org_id : int, optional
        The organization's id; the persisted row is listed on that org's
        audit page.
    org : str, optional
        The organization's slug, kept as a snapshot (a deleted org's events
        stay readable to instance admins).
    **fields
        More context (never a token, secret or password).
    """
    scope = getattr(request, "scope", request)
    host = ""
    for name, value in scope.get("headers") or ():
        if name == b"host":
            host = value.decode("latin-1")
            break
    client = scope.get("client")
    record = {
        "event": event,
        "uid": uid,
        "target": target,
        "ip": client[0] if client else None,
        "host": host,
    }
    if org_id is not None:
        record["org_id"] = org_id
    if org is not None:
        record["org"] = org
    record.update(fields)
    logger.info(
        "audit %s",
        " ".join(f"{k}={v}" for k, v in record.items() if v not in (None, "")),
        extra={"audit": record},
    )
    writer = _writer
    if writer is not None:
        writer.submit(
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            event=event,
            uid=uid,
            target=target,
            ip=record["ip"],
            org_id=org_id,
            org=org,
            fields=fields,
        )
