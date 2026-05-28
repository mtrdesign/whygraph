"""Idempotently add entries to a project's ``.gitignore``.

Used by ``whygraph init`` to keep the user-owned config and the generated
caches out of git: ``whygraph.toml`` (may hold API keys), ``.whygraph/``
and ``.codegraph/`` (regenerable SQLite). The committable
``whygraph.example.toml`` is intentionally *not* ignored.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def _normalize(line: str) -> str:
    """Reduce a gitignore line to its comparison key.

    Strips surrounding whitespace and a single trailing ``/`` so that
    ``.whygraph`` and ``.whygraph/`` are treated as the same entry.
    """
    return line.strip().rstrip("/")


def ensure_gitignore_entries(
    project_root: Path,
    entries: Sequence[str],
    *,
    header: str = "# WhyGraph",
) -> list[str]:
    """Append any missing ``entries`` to ``<project_root>/.gitignore``.

    Idempotent and slash-insensitive: an entry already present (matched by
    :func:`_normalize`, so trailing-slash variants count as equal) is never
    re-added. The file is created if absent; existing content is preserved
    verbatim. When something is appended and ``header`` is not already in
    the file, the ``header`` comment is written above the new block.

    Parameters
    ----------
    project_root : Path
        Directory containing (or to contain) the ``.gitignore``.
    entries : Sequence[str]
        Gitignore patterns to ensure are present, in order.
    header : str, default "# WhyGraph"
        Comment line written above a freshly appended block. Skipped if it
        already appears anywhere in the file.

    Returns
    -------
    list[str]
        The entries that were newly written (empty if all were present).
    """
    gitignore = project_root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""

    present = {
        _normalize(line)
        for line in existing.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    missing = [e for e in entries if _normalize(e) not in present]
    if not missing:
        return []

    block_lines: list[str] = []
    if header and header not in existing:
        block_lines.append(header)
    block_lines.extend(missing)
    block = "\n".join(block_lines) + "\n"

    if existing and not existing.endswith("\n"):
        existing += "\n"
    # A blank line before the block keeps it visually separate from prior
    # content (but not at the very top of a freshly created file).
    separator = "\n" if existing else ""
    gitignore.write_text(existing + separator + block, encoding="utf-8")
    return missing


__all__ = ["ensure_gitignore_entries"]
