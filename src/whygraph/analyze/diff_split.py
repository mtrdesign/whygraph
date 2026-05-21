"""Split an oversized git diff into per-file chunks.

When a diff is too large to describe in a single LLM round-trip it is
cut at file boundaries, and the resulting per-file segments are
bin-packed into chunks that each stay under a character budget. Each
chunk is then described independently and the descriptions synthesised
into one — see :class:`whygraph.analyze.LlmDescriptor`.

This module is pure: it knows nothing about git or the LLM, only about
the textual shape of unified-diff output. That keeps it trivially
testable — feed a diff string, assert on the returned chunks.
"""

from __future__ import annotations

import re

_FILE_HEADER = re.compile(r"^diff --git ", re.MULTILINE)
"""Matches the start of each file's section in ``git diff`` output."""


def _split_into_files(diff: str) -> list[str]:
    """Cut ``diff`` into one segment per file.

    Each segment spans from one ``diff --git`` header to the next (or to
    the end of the diff). Any text preceding the first header — unusual,
    but possible with non-standard diff output — is folded into the
    first segment so no input is dropped.

    Parameters
    ----------
    diff : str
        Raw unified-diff text.

    Returns
    -------
    list of str
        One segment per file, in diff order. When no ``diff --git``
        header is present (e.g. a binary-only or malformed diff), the
        whole ``diff`` is returned as a single segment.
    """
    starts = [match.start() for match in _FILE_HEADER.finditer(diff)]
    if not starts:
        return [diff]
    # Fold any preamble before the first header into the first segment.
    boundaries = [0, *starts[1:]]
    segments: list[str] = []
    for index, start in enumerate(boundaries):
        end = boundaries[index + 1] if index + 1 < len(boundaries) else len(diff)
        segments.append(diff[start:end])
    return segments


def split_into_chunks(diff: str, max_chars: int) -> list[str]:
    """Split ``diff`` at file boundaries and bin-pack into sized chunks.

    Consecutive file segments are accumulated into a chunk; a new chunk
    is started whenever appending the next segment would push the
    current one past ``max_chars``. A single file larger than
    ``max_chars`` lands in a chunk of its own — it is *not* split
    further, so the caller must still truncate an oversized chunk.

    Parameters
    ----------
    diff : str
        Raw unified-diff text.
    max_chars : int
        Soft cap on chunk size. A chunk is closed before it would
        exceed this, except a lone oversized file which cannot fit.

    Returns
    -------
    list of str
        Chunks in diff order, their concatenation equal to ``diff``.
        A diff with no recognisable file headers yields a single chunk.
    """
    chunks: list[str] = []
    current = ""
    for segment in _split_into_files(diff):
        if current and len(current) + len(segment) > max_chars:
            chunks.append(current)
            current = segment
        else:
            current += segment
    if current:
        chunks.append(current)
    return chunks
