"""In-memory value object for one file's change inside a single commit.

Exposes :class:`FileChange` plus the parser that builds a tuple of them
from ``git diff-tree --raw --numstat`` stdout. The parser lives here (not
on :class:`~whygraph.services.git.Repository`) so that "what diff-tree
output looks like" is owned by the class that represents it — the same
pattern :class:`~whygraph.services.git.commit.Commit` and
:class:`~whygraph.services.git.blame.BlameHunk` already follow.

The data is what powers Phase 2 of the layered evidence pipeline: every
``(commit, path-at-that-commit, change_type, renamed_from?)`` tuple
becomes a row in ``commit_file_change``, which in turn drives
rename-chain traversal and area-history queries that ``git blame``
cannot answer on its own.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FileChange:
    """One file's change as recorded inside a single commit.

    Attributes
    ----------
    path : str
        The file's path **as of this commit**. For renames and copies
        (``change_type`` in ``{"R", "C"}``), this is the *destination*
        path; the source path lives in :attr:`renamed_from`.
    change_type : str
        One-letter git change code: ``A`` (added), ``M`` (modified),
        ``D`` (deleted), ``R`` (renamed), ``C`` (copied), or ``T``
        (type change — rare; treated as a modification by callers).
    renamed_from : str or None
        The previous path when ``change_type`` is ``R`` or ``C``. ``None``
        for every other change type.
    similarity : int or None
        Git's similarity score (0–100) for ``R`` / ``C`` changes; ``None``
        otherwise. Useful for downstream consumers that want to weight
        high-similarity renames more than low-similarity copies.
    lines_added : int
        Lines added by this change (from ``--numstat``). ``0`` for binary
        files or pure renames with no body change.
    lines_deleted : int
        Lines deleted by this change. Same caveats as :attr:`lines_added`.
    """

    path: str
    change_type: str
    renamed_from: str | None
    similarity: int | None
    lines_added: int
    lines_deleted: int

    @classmethod
    def from_diff_tree(cls, stdout: str) -> tuple["FileChange", ...]:
        """Parse ``git diff-tree --raw --numstat`` output into per-file records.

        ``git diff-tree -r -M -C --no-commit-id --root --raw --numstat <sha>``
        emits two adjacent blocks for one commit: a raw block (each line
        prefixed with ``:`` and tab-separated) followed by a numstat block
        (``<added>\\t<deleted>\\t<path>``). This parser walks both,
        pairing them by destination path, so a single call returns one
        :class:`FileChange` per file the commit touched.

        Parameters
        ----------
        stdout : str
            Raw stdout of ``git diff-tree -r -M -C --no-commit-id --root
            --raw --numstat <sha>``.

        Returns
        -------
        tuple[FileChange, ...]
            One entry per touched file, in the order git emitted them.

        Notes
        -----
        Binary files surface in ``--numstat`` with a literal ``-`` for
        both line counts; this parser maps those to zero rather than
        propagating a separate "binary" flag. Downstream consumers
        already treat ``(0, 0)`` for a modified file as "no measurable
        body change", which matches the intent.
        """
        raw_records: list[dict] = []
        numstat: dict[str, tuple[int, int]] = {}
        for line in stdout.splitlines():
            if not line:
                continue
            if line.startswith(":"):
                raw_records.append(_parse_raw_line(line))
            else:
                parsed = _parse_numstat_line(line)
                if parsed is not None:
                    added, deleted, new_path = parsed
                    numstat[new_path] = (added, deleted)

        out: list[FileChange] = []
        for rec in raw_records:
            added, deleted = numstat.get(rec["path"], (0, 0))
            out.append(
                cls(
                    path=rec["path"],
                    change_type=rec["change_type"],
                    renamed_from=rec["renamed_from"],
                    similarity=rec["similarity"],
                    lines_added=added,
                    lines_deleted=deleted,
                )
            )
        return tuple(out)


def _parse_raw_line(line: str) -> dict:
    """Parse one ``--raw`` line into a record dict.

    Raw format is ``:<mode_src> <mode_dst> <sha_src> <sha_dst> <status>[<sim>]\\t<path>[\\t<newpath>]``.
    The status is one letter plus an optional similarity score for R/C.
    """
    fields = line.split("\t")
    head = fields[0].split()
    status = head[-1]
    paths = fields[1:]
    if status and status[0] in ("R", "C"):
        change_type = status[0]
        similarity: int | None = int(status[1:]) if status[1:].isdigit() else None
        renamed_from: str | None = paths[0] if len(paths) >= 1 else None
        new_path = paths[1] if len(paths) >= 2 else (paths[0] if paths else "")
    else:
        change_type = status or "M"
        similarity = None
        renamed_from = None
        new_path = paths[0] if paths else ""
    return {
        "change_type": change_type,
        "renamed_from": renamed_from,
        "similarity": similarity,
        "path": new_path,
    }


def _parse_numstat_line(line: str) -> tuple[int, int, str] | None:
    """Parse one ``--numstat`` line into ``(added, deleted, new_path)``.

    Binary files emit ``-\\t-\\t<path>`` and are mapped to ``(0, 0)``.
    Renames may appear as ``<a>\\t<d>\\t<old> => <new>`` or with a brace
    form when the parent directories overlap (e.g.
    ``<a>\\t<d>\\tsrc/{old.py => new.py}``); both collapse to the new
    path.
    """
    parts = line.split("\t", 2)
    if len(parts) != 3:
        return None
    added_str, deleted_str, raw_path = parts
    added = int(added_str) if added_str.isdigit() else 0
    deleted = int(deleted_str) if deleted_str.isdigit() else 0
    new_path = _collapse_rename_arrow(raw_path)
    return added, deleted, new_path


def _collapse_rename_arrow(raw_path: str) -> str:
    """Extract the destination path from a ``--numstat`` rename token.

    Three shapes show up in practice:

    * ``foo.py`` — unchanged path, return as is.
    * ``old.py => new.py`` — plain arrow form, return ``new.py``.
    * ``src/{old.py => new.py}`` — brace form when parent dirs match,
      return ``src/new.py``.
    """
    if " => " not in raw_path:
        return raw_path
    if "{" in raw_path and "}" in raw_path:
        prefix, rest = raw_path.split("{", 1)
        inside, suffix = rest.split("}", 1)
        _, new_inside = inside.split(" => ", 1)
        return prefix + new_inside + suffix
    _, new_path = raw_path.split(" => ", 1)
    return new_path
