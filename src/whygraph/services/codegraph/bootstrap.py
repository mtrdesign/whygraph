"""Bootstrap and refresh CodeGraph's ``.codegraph/codegraph.db``.

WhyGraph reads CodeGraph's SQLite directly (see :mod:`.graph`), but it
doesn't ship CodeGraph itself. Two execution paths cover both how
WhyGraph is run:

* **Local binary.** When a ``codegraph`` executable is on ``PATH`` —
  notably inside the WhyGraph runtime container, which bakes in Node +
  the pinned npm package — it is invoked directly. No Docker, so this
  works headless and avoids docker-in-docker. This is the only path the
  container delivery ever takes.
* **Docker fallback.** On a native (e.g. ``uv tool install``) host
  without ``codegraph`` installed, the WhyGraph image
  (``ghcr.io/mtrdesign/whygraph``) is run as ``docker run … codegraph
  …`` with the project root bind-mounted to ``/workspace``. That single
  image already carries the right Node version and the pinned upstream
  package, so the host only needs Docker.

:func:`ensure_codegraph_db` is idempotent (used by ``whygraph init``);
:func:`refresh_codegraph_index` re-syncs an existing index (used by
``whygraph scan``). Both are usable standalone (e.g. from tests).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .exceptions import CodeGraphBootstrapError
from .paths import CODEGRAPH_DB_RELPATH

DEFAULT_CODEGRAPH_IMAGE: str = "ghcr.io/mtrdesign/whygraph:latest"
"""Default image run by the Docker fallback when no override is given.

The self-contained WhyGraph image bakes in the CodeGraph CLI, so the
fallback runs ``codegraph`` inside it. ``whygraph scan`` exposes
``--codegraph-image`` for ad-hoc overrides; tests and CI pipelines can
also pass an explicit ``image=`` to the functions here. Ignored when a
local ``codegraph`` binary is used instead.
"""


def ensure_codegraph_db(
    project_root: Path,
    *,
    image: str | None = None,
    capture: bool = False,
) -> Path:
    """Idempotently materialize ``<project_root>/.codegraph/codegraph.db``.

    If the database already exists, returns its absolute path immediately.
    Otherwise runs ``codegraph init -i`` (initialize + initial index) via
    the local binary if present, else the vendored Docker image.

    Parameters
    ----------
    project_root : Path
        Repository root — the directory that should end up containing
        ``.codegraph/``.
    image : str, optional
        Docker image tag for the fallback path. Defaults to
        :data:`DEFAULT_CODEGRAPH_IMAGE`.
    capture : bool, optional
        When ``True``, capture the subprocess output instead of letting it
        stream to the terminal (so it can't corrupt a concurrent progress
        display). The captured tail is folded into the error message on
        failure. Default ``False`` (stream live).

    Returns
    -------
    Path
        Absolute path to ``<project_root>/.codegraph/codegraph.db``.

    Raises
    ------
    CodeGraphBootstrapError
        If neither ``codegraph`` nor ``docker`` is on PATH, the command
        exits non-zero, or the database is still missing afterward.
    """
    project_root = project_root.resolve()
    db_path = project_root / CODEGRAPH_DB_RELPATH
    if db_path.exists():
        return db_path

    _run_codegraph(project_root, ["init", "-i"], image=image, capture=capture)

    if not db_path.exists():
        raise CodeGraphBootstrapError(
            f"codegraph exited cleanly but {db_path} was not created"
            " — check codegraph output above for errors"
        )
    return db_path


def refresh_codegraph_index(
    project_root: Path,
    *,
    image: str | None = None,
    capture: bool = False,
) -> Path:
    """Bring ``<project_root>/.codegraph/codegraph.db`` up to date.

    When the database is missing, behaves like :func:`ensure_codegraph_db`
    (full ``init -i``). When it already exists, runs ``codegraph sync -q``
    — an incremental update of just the changes since the last index,
    which is what ``whygraph scan`` wants on each run.

    Parameters
    ----------
    project_root : Path
        Repository root containing (or to contain) ``.codegraph/``.
    image : str, optional
        Docker image tag for the fallback path. Defaults to
        :data:`DEFAULT_CODEGRAPH_IMAGE`.
    capture : bool, optional
        When ``True``, capture the subprocess output instead of streaming
        it (see :func:`ensure_codegraph_db`). ``whygraph scan`` passes this
        so the refresh can run concurrently under a live progress display.
        Default ``False``.

    Returns
    -------
    Path
        Absolute path to ``<project_root>/.codegraph/codegraph.db``.

    Raises
    ------
    CodeGraphBootstrapError
        If neither ``codegraph`` nor ``docker`` is on PATH, the command
        exits non-zero, or the database is still missing afterward.
    """
    project_root = project_root.resolve()
    db_path = project_root / CODEGRAPH_DB_RELPATH
    if not db_path.exists():
        return ensure_codegraph_db(project_root, image=image, capture=capture)

    _run_codegraph(project_root, ["sync", "-q"], image=image, capture=capture)
    return db_path


def _run_codegraph(
    project_root: Path,
    args: list[str],
    *,
    image: str | None,
    capture: bool = False,
) -> None:
    """Run a ``codegraph`` subcommand against ``project_root``.

    Prefers a local ``codegraph`` binary (the container path — no Docker);
    falls back to running ``codegraph`` inside the WhyGraph image,
    bind-mounting the project root. The Docker invocation is
    non-interactive (no ``-t``) so it works under ``docker exec`` and in CI.

    Parameters
    ----------
    project_root : Path
        Resolved repository root.
    args : list of str
        The ``codegraph`` subcommand and flags (e.g. ``["init", "-i"]``).
    image : str or None
        Docker image tag for the fallback path; ``None`` uses
        :data:`DEFAULT_CODEGRAPH_IMAGE`.
    capture : bool, optional
        When ``True``, capture stdout/stderr rather than streaming them to
        the terminal, and fold the captured tail into the error message on
        failure. Default ``False`` (stream live).

    Raises
    ------
    CodeGraphBootstrapError
        If neither tool is on PATH or the command exits non-zero.
    """
    label = " ".join(args)
    if shutil.which("codegraph") is not None:
        cmd = ["codegraph", *args]
        cwd: Path | None = project_root
    elif shutil.which("docker") is not None:
        img = image or DEFAULT_CODEGRAPH_IMAGE
        # The WhyGraph image has no ENTRYPOINT (its CMD is `whygraph
        # --help`), so name the `codegraph` binary explicitly.
        cmd = [
            "docker",
            "run",
            "--rm",
            "-i",
            *_user_arg(),
            "-v",
            f"{project_root}:/workspace",
            "-w",
            "/workspace",
            img,
            "codegraph",
            *args,
        ]
        cwd = None
    else:
        raise CodeGraphBootstrapError(
            "neither `codegraph` nor `docker` is on PATH — install the"
            " CodeGraph CLI (Node ≥ 22) or Docker Desktop and re-run, or"
            " pass --no-codegraph to skip the CodeGraph step"
        )

    try:
        subprocess.run(cmd, check=True, cwd=cwd, capture_output=capture, text=capture)
    except subprocess.CalledProcessError as exc:
        if capture:
            tail = (exc.stderr or exc.stdout or "").strip()
            detail = f"\n{tail}" if tail else ""
            raise CodeGraphBootstrapError(
                f"`codegraph {label}` failed (exit {exc.returncode}){detail}"
            ) from exc
        raise CodeGraphBootstrapError(
            f"`codegraph {label}` failed (exit {exc.returncode}) — see output above"
        ) from exc


def _user_arg() -> list[str]:
    # Pass --user uid:gid so bind-mounted files come back owned by the host
    # user. Windows lacks os.getuid/getgid; on those platforms we omit the
    # flag and trust Docker Desktop's VFS to handle ownership.
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        return ["--user", f"{os.getuid()}:{os.getgid()}"]
    return []


__all__ = [
    "DEFAULT_CODEGRAPH_IMAGE",
    "ensure_codegraph_db",
    "refresh_codegraph_index",
]
