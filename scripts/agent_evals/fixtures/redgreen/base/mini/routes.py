"""A tiny router: each route maps to a handler, as a web framework does."""

import inspect

NOTES = [
    {"id": 1, "body": "a constraint", "kind": "constraint"},
    {"id": 2, "body": "a gotcha", "kind": "gotcha"},
]


def list_notes(limit: int = 100) -> list[dict]:
    """Return the notes, newest first."""
    return sorted(NOTES, key=lambda note: note["id"], reverse=True)[:limit]


ROUTES = {("GET", "/notes"): list_notes}


def handle(method: str, path: str, params: dict | None = None) -> list[dict]:
    """Call a route's handler with the query parameters it declares.

    Like a web framework, it ignores a query parameter the handler does not take.
    """
    handler = ROUTES[(method, path)]
    accepted = inspect.signature(handler).parameters
    return handler(**{k: v for k, v in (params or {}).items() if k in accepted})
