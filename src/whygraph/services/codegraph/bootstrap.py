"""Bootstrap CodeGraph's ``.codegraph/codegraph.db`` via a vendored Docker image.

WhyGraph reads CodeGraph's SQLite directly (see :mod:`.graph`), but it
doesn't ship CodeGraph itself. Historically users had to run
``codegraph init -i`` by hand against a Node ≥ 22 install on their host
— brittle on machines where the default ``node`` is older.

This module replaces that with a single :func:`ensure_codegraph_db` call
that runs the vendored ``ghcr.io/mtrdesign/whygraph-codegraph`` image with
the project root bind-mounted to ``/workspace``. The container bakes in
the right Node version and pins the upstream npm package; the host only
needs Docker.

The helper is idempotent — if the database already exists it returns
immediately. Designed to be called from ``whygraph init`` but usable
standalone (e.g. from tests).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .exceptions import CodeGraphBootstrapError
from .paths import CODEGRAPH_DB_RELPATH

DEFAULT_CODEGRAPH_IMAGE: str = "ghcr.io/mtrdesign/whygraph-codegraph:latest"
"""Default tag pulled by :func:`ensure_codegraph_db` when no override is given.

The CLI exposes ``--codegraph-image`` for ad-hoc overrides; tests and
CI pipelines can also pass an explicit ``image=`` to the function.
"""


def ensure_codegraph_db(
    project_root: Path,
    *,
    image: str | None = None,
) -> Path:
    """Idempotently materialize ``<project_root>/.codegraph/codegraph.db``.

    If the database already exists, returns its absolute path immediately.
    Otherwise runs the vendored Docker image (``codegraph init -i``) with
    the project root bind-mounted to ``/workspace`` and the host UID/GID
    propagated, so the resulting files belong to the calling user.

    Parameters
    ----------
    project_root : Path
        Repository root — the directory that should end up containing
        ``.codegraph/``.
    image : str, optional
        Docker image tag to run. Defaults to :data:`DEFAULT_CODEGRAPH_IMAGE`.

    Returns
    -------
    Path
        Absolute path to ``<project_root>/.codegraph/codegraph.db``.

    Raises
    ------
    CodeGraphBootstrapError
        If ``docker`` is not on PATH, the container exits non-zero, or
        the database is still missing after a successful container exit.
    """
    project_root = project_root.resolve()
    db_path = project_root / CODEGRAPH_DB_RELPATH
    if db_path.exists():
        return db_path

    if shutil.which("docker") is None:
        raise CodeGraphBootstrapError(
            "docker is not on PATH — install Docker Desktop and re-run, "
            "or pass --no-codegraph to skip the CodeGraph bootstrap"
        )

    img = image or DEFAULT_CODEGRAPH_IMAGE
    cmd = [
        "docker",
        "run",
        "--rm",
        "-it",
        *_user_arg(),
        "-v",
        f"{project_root}:/workspace",
        "-w",
        "/workspace",
        img,
        "init",
        "-i",
    ]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise CodeGraphBootstrapError(
            f"codegraph init failed inside container (exit {exc.returncode})"
            " — see output above"
        ) from exc

    if not db_path.exists():
        raise CodeGraphBootstrapError(
            f"container exited cleanly but {db_path} was not created"
            " — check codegraph output above for errors"
        )
    return db_path


def _user_arg() -> list[str]:
    # Pass --user uid:gid so bind-mounted files come back owned by the host
    # user. Windows lacks os.getuid/getgid; on those platforms we omit the
    # flag and trust Docker Desktop's VFS to handle ownership.
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        return ["--user", f"{os.getuid()}:{os.getgid()}"]
    return []


__all__ = ["DEFAULT_CODEGRAPH_IMAGE", "ensure_codegraph_db"]
