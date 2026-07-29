"""Read-only, clamped file access for the chat assistant.

Without raw source the assistant cannot genuinely debug — it can describe
a symbol's history and relationships but never read the line that is
actually wrong. So the tool set includes ``read_file`` / ``list_dir``.
This module is the whole security surface those two tools add, and it is
deliberately paranoid:

* **Root clamp.** Every path resolves against the repo root and must stay
  inside it (``Path.is_relative_to``), which rejects ``../`` escapes and
  absolute paths alike.
* **Deny-list inside the root.** Being inside the repo is not sufficient.
  ``whygraph.toml`` holds live API keys and a GitHub token; ``.git`` /
  ``.whygraph`` / ``.codegraph`` hold history and databases; ``.env*``
  holds secrets by convention. None of that may ever reach a transcript,
  which is a place secrets would then be *persisted*.
* **Binary refusal.** A null byte in the first block means "not source",
  and dumping a binary into the context window is pure waste.
* **Size caps.** 400 lines / 100 KB per call, so one ``read_file`` on a
  generated file cannot swamp the context window.

Every refusal is a *returned value*, never an exception — the model reads
the reason and adjusts. Only genuinely unexpected failures propagate.
"""

from __future__ import annotations

from pathlib import Path

from whygraph.mcp.targets import repo_root

MAX_LINES = 400
"""Cap on lines returned by one :func:`read_file` call."""

MAX_BYTES = 100_000
"""Cap on bytes read by one :func:`read_file` call."""

MAX_ENTRIES = 200
"""Cap on entries returned by one :func:`list_dir` call."""

_DENIED_PARTS = frozenset({".git", ".whygraph", ".codegraph"})
"""Directory names that are off-limits anywhere in a path.

``.git`` and ``.whygraph`` / ``.codegraph`` are WhyGraph's own storage —
the assistant reaches their content through the knowledge tools, which
present it in a curated shape, not by reading raw databases.
"""

_DENIED_NAMES = frozenset({"whygraph.toml"})
"""Exact file names that are off-limits: the live config holds real keys."""


def _is_denied(relative: Path) -> str | None:
    """Return a refusal reason for ``relative``, or ``None`` if allowed.

    ``relative`` must already be repo-root-relative. Checks apply to every
    path component, so ``a/.git/config`` and ``.git/config`` both refuse.
    """
    parts = relative.parts
    for part in parts:
        if part in _DENIED_PARTS:
            return f"{part!r} is WhyGraph-internal — use the knowledge tools instead"
    name = relative.name
    if name in _DENIED_NAMES:
        return f"{name!r} holds live API keys and is never readable"
    if name.startswith(".env"):
        return f"{name!r} holds secrets and is never readable"
    return None


def _resolve_in_root(path: str) -> tuple[Path, Path] | str:
    """Resolve ``path`` under the repo root.

    Returns
    -------
    tuple[Path, Path] or str
        ``(absolute, relative_to_root)`` when the path is inside the root
        and passes the deny-list; otherwise a refusal string.
    """
    root = repo_root().resolve()
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        return f"path {path!r} is outside the repository — refused"
    relative = candidate.relative_to(root)
    denied = _is_denied(relative)
    if denied is not None:
        return f"refused: {denied}"
    return candidate, relative


def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> dict:
    """Read a slice of a repository file, with line numbers.

    Parameters
    ----------
    path : str
        Repo-root-relative path. Absolute paths and ``../`` escapes are
        refused.
    start_line : int, optional
        First line to return, 1-based inclusive. Default ``1``. Values
        below 1 are clamped up rather than refused — an off-by-one from
        the model shouldn't cost a whole tool round.
    end_line : int or None, optional
        Last line to return, 1-based inclusive. ``None`` (default) reads
        :data:`MAX_LINES` lines from ``start_line``. A range wider than
        :data:`MAX_LINES` is truncated, not refused.

    Returns
    -------
    dict
        ``{path, start_line, end_line, total_lines, truncated, content}``
        where ``content`` is newline-joined ``"<n>→<text>"`` rows — or
        ``{"error": "<reason>"}`` for any refusal (outside the root,
        deny-listed, missing, a directory, or binary).
    """
    resolved = _resolve_in_root(path)
    if isinstance(resolved, str):
        return {"error": resolved}
    absolute, relative = resolved

    if not absolute.exists():
        return {"error": f"{path!r} does not exist"}
    if absolute.is_dir():
        return {"error": f"{path!r} is a directory — use list_dir"}

    try:
        raw = absolute.read_bytes()[:MAX_BYTES]
    except OSError as exc:
        return {"error": f"cannot read {path!r}: {exc}"}

    if b"\x00" in raw[:8192]:
        return {"error": f"{path!r} looks binary — refused"}

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()

    first = max(1, start_line)
    last = (
        first + MAX_LINES - 1
        if end_line is None
        else min(end_line, first + MAX_LINES - 1)
    )
    last = min(last, len(lines))
    if first > len(lines):
        return {
            "error": (
                f"start_line {start_line} is past the end of {path!r} "
                f"({len(lines)} lines)"
            )
        }

    numbered = "\n".join(f"{n}→{lines[n - 1]}" for n in range(first, last + 1))
    return {
        "path": str(relative),
        "start_line": first,
        "end_line": last,
        "total_lines": len(lines),
        # True when the caller is not seeing the whole file, for either
        # reason (byte cap hit, or the requested range is a slice).
        "truncated": len(raw) == MAX_BYTES or last < len(lines) or first > 1,
        "content": numbered,
    }


def list_dir(path: str = ".") -> dict:
    """List one directory's entries, non-recursively.

    Parameters
    ----------
    path : str, optional
        Repo-root-relative directory. Default ``"."`` (the repo root).

    Returns
    -------
    dict
        ``{path, entries, truncated}`` where ``entries`` is a sorted list
        of names with ``/`` appended to directories — or
        ``{"error": "<reason>"}``. Deny-listed children are silently
        omitted rather than listed-but-unreadable, so the model does not
        waste a round asking for them.
    """
    resolved = _resolve_in_root(path)
    if isinstance(resolved, str):
        return {"error": resolved}
    absolute, relative = resolved

    if not absolute.exists():
        return {"error": f"{path!r} does not exist"}
    if not absolute.is_dir():
        return {"error": f"{path!r} is not a directory — use read_file"}

    try:
        children = sorted(absolute.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    except OSError as exc:
        return {"error": f"cannot list {path!r}: {exc}"}

    entries: list[str] = []
    for child in children:
        if _is_denied(relative / child.name) is not None:
            continue
        entries.append(f"{child.name}/" if child.is_dir() else child.name)

    return {
        "path": str(relative),
        "entries": entries[:MAX_ENTRIES],
        "truncated": len(entries) > MAX_ENTRIES,
    }


__all__ = ["MAX_BYTES", "MAX_ENTRIES", "MAX_LINES", "list_dir", "read_file"]
