"""The security event log.

One structured ``INFO`` record per security-relevant event on the
``whygraph.portal.audit`` logger. Callers pass identifiers only: never a
token, secret or password.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("whygraph.portal.audit")


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
        **fields,
    }
    logger.info(
        "audit %s",
        " ".join(f"{k}={v}" for k, v in record.items() if v not in (None, "")),
        extra={"audit": record},
    )
