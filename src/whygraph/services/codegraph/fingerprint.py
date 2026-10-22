"""What a CodeGraph index refresh of a repository would change.

The portal's CodeGraph bridge refreshes a project's index before it answers
a call, but only when the repository moved since the last refresh: this
module fingerprints that state cheaply (``HEAD``, ``git status`` and the
``(mtime_ns, size)`` of every listed path). The scan runner takes the same
fingerprint before a scan child starts, so a scan whose CodeGraph step ended
OK spares the next call a refresh.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from whygraph.core.safe_paths import UnsafePathError, check_inside
from whygraph.services.git.credentials import git_env

FINGERPRINT_TIMEOUT_SEC: float = 5.0
"""Seconds a whole fingerprint may take."""


FINGERPRINT_GIT_FLAGS: tuple[str, ...] = (
    "--no-optional-locks",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.checkStat=minimal",
    "-c",
    "core.trustctime=false",
)
"""Global git options of every fingerprint command. A bind-mounted repository's
index is written by host git and its stat cache never matches inside the
container; ``checkStat=minimal`` / ``trustctime=false`` keep git from
re-hashing every tracked file (and ``--no-optional-locks`` from taking
``.git/index.lock``)."""

_OWN_DIRS: tuple[str, ...] = (":(exclude).codegraph", ":(exclude).whygraph")
"""Pathspecs leaving out what the portal itself writes (an index refresh
must not change the fingerprint it is gated by)."""


class FingerprintError(Exception):
    """A fingerprint command failed or ran out of time."""


@dataclass(frozen=True)
class Fingerprint:
    """A repository's state as far as an index refresh is concerned.

    Attributes
    ----------
    digest : str
        SHA-256 over ``HEAD``, the ``git status`` listing and the
        ``(mtime_ns, size)`` of every listed path.
    head : str or None
        ``HEAD``'s commit (``None`` before the first commit).
    dirty : int
        Paths ``git status`` listed.
    """

    digest: str
    head: str | None
    dirty: int


def _git_out(
    root: Path, args: list[str], deadline: float, *, ok: tuple[int, ...] = (0,)
) -> bytes:
    """Run one fingerprint git command under ``git_env``; its stdout."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FingerprintError("timed out")
    try:
        proc = subprocess.run(
            ["git", *FINGERPRINT_GIT_FLAGS, *args],
            cwd=root,
            env=git_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=remaining,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FingerprintError("timed out") from exc
    except OSError as exc:
        raise FingerprintError(type(exc).__name__) from exc
    if proc.returncode not in ok:
        raise FingerprintError(f"git {args[0]} exited {proc.returncode}")
    return proc.stdout


def _status_paths(raw: bytes) -> tuple[list[str], int]:
    """The paths of a ``git status --porcelain=v1 -z`` listing, and its entry count."""
    fields = raw.split(b"\0")
    paths: list[str] = []
    entries = 0
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        entries += 1
        paths.append(os.fsdecode(entry[3:]))
        if (entry[0] in b"RC" or entry[1] in b"RC") and i < len(fields):
            paths.append(os.fsdecode(fields[i]))  # a rename's or copy's source
            i += 1
    return paths, entries


def _stat_key(root: Path, rel: str, parents: dict[str, Path | None]) -> bytes:
    """``mtime_ns:size`` of one listed path, read without following any link."""
    parent_rel, _, name = rel.rstrip("/").rpartition("/")
    if parent_rel not in parents:
        try:
            parents[parent_rel] = check_inside(root, parent_rel or ".")
        except UnsafePathError:
            parents[parent_rel] = None
    parent = parents[parent_rel]
    if parent is None:
        return b"unsafe"
    try:
        st = os.lstat(parent / name)  # never follows the last component
    except FileNotFoundError:
        return b"missing"
    except OSError:
        return b"error"
    return f"{st.st_mtime_ns}:{st.st_size}".encode()


def fingerprint(root: Path, *, timeout: float = FINGERPRINT_TIMEOUT_SEC) -> Fingerprint:
    """Fingerprint what an index refresh of ``root`` would read (blocking).

    ``git rev-parse HEAD`` + ``git status --porcelain=v1 -z
    --untracked-files=all`` (both with :data:`FINGERPRINT_GIT_FLAGS`, under
    :func:`~whygraph.services.git.credentials.git_env`, without
    ``.codegraph/`` and ``.whygraph/``) + the ``(mtime_ns, size)`` of every
    listed path (``lstat`` below a parent that passed ``check_inside``), so
    a re-edit of an already dirty file changes it too.

    Parameters
    ----------
    root : Path
        The repository root.
    timeout : float, optional
        Seconds for the whole fingerprint.

    Returns
    -------
    Fingerprint
        The digest, ``HEAD`` and the number of listed paths.

    Raises
    ------
    FingerprintError
        A git command failed (e.g. dubious ownership) or the time ran out.
    """
    deadline = time.monotonic() + timeout
    head = (
        _git_out(root, ["rev-parse", "-q", "--verify", "HEAD"], deadline, ok=(0, 1))
        .strip()
        .decode("ascii", "replace")
    ) or None
    status = _git_out(
        root,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            ".",
            *_OWN_DIRS,
        ],
        deadline,
    )
    digest = hashlib.sha256((head or "-").encode() + b"\0" + status)
    paths, entries = _status_paths(status)
    parents: dict[str, Path | None] = {}
    for rel in paths:
        if time.monotonic() > deadline:
            raise FingerprintError("timed out")
        digest.update(b"\0" + os.fsencode(rel) + b"\0" + _stat_key(root, rel, parents))
    return Fingerprint(digest.hexdigest(), head, entries)


def changed_files(
    root: Path, before: Fingerprint | None, now: Fingerprint | None
) -> int:
    """About how many files a refresh from ``before`` to ``now`` reads (blocking).

    The dirty paths of ``now``, plus the files changed between the two
    ``HEAD`` s when they differ (a branch switch leaves a clean tree). Any
    failure counts the dirty paths only.
    """
    if now is None:
        return 0
    count = now.dirty
    if before is None or not before.head or not now.head or before.head == now.head:
        return count
    try:
        out = _git_out(
            root,
            ["diff", "--name-only", "-z", before.head, now.head, "--"],
            time.monotonic() + FINGERPRINT_TIMEOUT_SEC,
        )
    except FingerprintError:
        return count
    return count + sum(1 for name in out.split(b"\0") if name)


__all__ = [
    "FINGERPRINT_GIT_FLAGS",
    "FINGERPRINT_TIMEOUT_SEC",
    "Fingerprint",
    "FingerprintError",
    "changed_files",
    "fingerprint",
]
