"""In-memory value object for ``git blame`` output — one hunk per commit.

Exposes :class:`BlameHunk` (with its :class:`BlameOrigin` line ranges) plus
the parser that builds a tuple of them from
``git blame --porcelain`` stdout. The parser lives here (not on
:class:`~whygraph.services.git.Repository`) so that "what blame output looks
like" is owned by the class that represents it — the same pattern
:meth:`whygraph.services.git.Commit.from_git_log` follows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Git's sentinel SHA for lines that are not yet committed (local edits).
_UNCOMMITTED_SHA = "0" * 40

# C-style escapes git uses when it quotes an unusual path (``quote_c_style``).
_C_ESCAPES = {
    "a": 7,
    "b": 8,
    "t": 9,
    "n": 10,
    "v": 11,
    "f": 12,
    "r": 13,
    '"': 34,
    "\\": 92,
}


def _unquote_path(raw: str) -> str:
    """Undo git's C-style quoting of a path (``"dir/\\303\\251.py"``)."""
    if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
        return raw
    body = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            digits = body[i + 1 : i + 4]
            if len(digits) == 3 and all(d in "01234567" for d in digits):
                out.append(int(digits, 8) & 0xFF)
                i += 4
                continue
            if nxt in _C_ESCAPES:
                out.append(_C_ESCAPES[nxt])
                i += 2
                continue
        out.extend(ch.encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="surrogateescape")


@dataclass(frozen=True, slots=True)
class BlameOrigin:
    """A contiguous line range a commit owns, as the file stood at that commit.

    ``git blame`` reports, for every blamed line, the path and line number
    the line had *in the owning commit* - which differs from the blamed
    file when the line was moved, copied or renamed since. Blaming
    ``path`` lines ``start``-``end`` at the owning commit's SHA reaches
    exactly these lines again, without the working tree.

    Attributes
    ----------
    path : str
        File path at the owning commit, relative to the repository root.
    start : int
        First line of the range in that file (1-based, inclusive).
    end : int
        Last line of the range in that file (1-based, inclusive).
    final_start : int
        The line of the blamed file (the working tree, or the blamed
        revision) where the range's first line sits. Used only for
        ordering.
    """

    path: str
    start: int
    end: int
    final_start: int


@dataclass(frozen=True, slots=True)
class BlameHunk:
    """The lines a single commit owns within a blamed range.

    ``git blame --porcelain`` reports one record per source line; this
    aggregates every line attributed to the same commit into one hunk, so a
    blamed range of N lines spanning M commits yields M hunks.

    Attributes
    ----------
    sha : str
        Full commit SHA. The all-zero SHA marks lines that are not yet
        committed (uncommitted local edits) — see :attr:`is_uncommitted`.
    lines_owned : int
        How many lines of the blamed range this commit is responsible for.
    author_name : str or None
        Commit author display name, when git reported it.
    author_email : str or None
        Commit author email, when git reported it.
    summary : str or None
        First line of the commit message, when git reported it.
    committed_at : str or None
        ISO 8601 UTC timestamp of when the commit was applied, derived from
        the porcelain ``committer-time`` epoch.
    origins : tuple[BlameOrigin, ...]
        The line ranges this commit owns, at the commit itself (path and
        line numbers as git reported them), in blame-output order with
        consecutive lines merged. Empty for a hunk built by hand.
    """

    sha: str
    lines_owned: int
    author_name: str | None
    author_email: str | None
    summary: str | None
    committed_at: str | None
    origins: tuple[BlameOrigin, ...] = ()

    @property
    def is_uncommitted(self) -> bool:
        """``True`` when this hunk covers uncommitted local edits."""
        return self.sha == _UNCOMMITTED_SHA

    @classmethod
    def from_porcelain(cls, stdout: str) -> tuple["BlameHunk", ...]:
        """Parse ``git blame --porcelain`` output into per-commit hunks.

        In porcelain format every source line emits a header line —
        ``<sha> <orig-line> <final-line> [<group-size>]`` — but the metadata
        block (``author``, ``summary``, ``committer-time``, …) is emitted
        only the *first* time a SHA appears. This parser carries the
        per-SHA metadata forward and tallies one owned line per header.

        The ``filename`` line is per **group** (a header carrying a
        ``<group-size>``): git emits it after the first header of a commit
        and after every group header once that commit's lines come from
        more than one path. A group without one inherits the last path
        seen for its SHA. Each line's ``<orig-line>`` is collected into
        :attr:`BlameHunk.origins` under its group's path, merging
        consecutive lines into one range.

        Parameters
        ----------
        stdout : str
            Raw stdout of ``git blame --porcelain``.

        Returns
        -------
        tuple[BlameHunk, ...]
            One hunk per distinct commit, in first-appearance order.
        """
        hunks: dict[str, dict] = {}
        order: list[str] = []
        last_path: dict[str, str] = {}
        current: str | None = None
        # The group in progress: its SHA, its ``filename`` (when git sent
        # one) and its (orig-line, final-line) pairs. Origins are attached
        # when the group closes, because ``filename`` follows the header.
        group: dict | None = None

        def close_group() -> None:
            if group is None:
                return
            path = group["path"] or last_path.get(group["sha"])
            if path is None:
                return
            last_path[group["sha"]] = path
            origins: list[BlameOrigin] = hunks[group["sha"]]["origins"]
            for orig, final in group["lines"]:
                prev = origins[-1] if origins else None
                if prev is not None and prev.path == path and prev.end + 1 == orig:
                    origins[-1] = BlameOrigin(path, prev.start, orig, prev.final_start)
                else:
                    origins.append(BlameOrigin(path, orig, orig, final))

        for line in stdout.splitlines():
            if line.startswith("\t"):
                # Source-line content, not metadata.
                continue
            parts = line.split(" ")
            if (
                len(parts) >= 3
                and len(parts[0]) == 40
                and parts[1].isdigit()
                and parts[2].isdigit()
            ):
                current = parts[0]
                entry = hunks.get(current)
                if entry is None:
                    entry = {
                        "sha": current,
                        "lines_owned": 0,
                        "author_name": None,
                        "author_email": None,
                        "summary": None,
                        "committed_at": None,
                        "origins": [],
                    }
                    hunks[current] = entry
                    order.append(current)
                entry["lines_owned"] += 1
                if group is None or len(parts) >= 4 or group["sha"] != current:
                    close_group()
                    group = {"sha": current, "path": None, "lines": []}
                group["lines"].append((int(parts[1]), int(parts[2])))
                continue
            if current is None:
                continue
            entry = hunks[current]
            if line.startswith("author "):
                entry["author_name"] = line[len("author ") :].strip() or None
            elif line.startswith("author-mail "):
                mail = line[len("author-mail ") :].strip()
                if mail.startswith("<") and mail.endswith(">"):
                    mail = mail[1:-1]
                entry["author_email"] = mail or None
            elif line.startswith("summary "):
                entry["summary"] = line[len("summary ") :].strip() or None
            elif line.startswith("committer-time "):
                epoch = line[len("committer-time ") :].strip()
                if epoch.isdigit():
                    entry["committed_at"] = datetime.fromtimestamp(
                        int(epoch), tz=timezone.utc
                    ).isoformat()
            elif line.startswith("filename ") and group is not None:
                group["path"] = _unquote_path(line[len("filename ") :])
        close_group()
        return tuple(
            BlameHunk(**{**hunks[sha], "origins": tuple(hunks[sha]["origins"])})
            for sha in order
        )
