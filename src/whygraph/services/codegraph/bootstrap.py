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

:func:`ensure_codegraph_db` is idempotent (the 1.x init step used it);
:func:`refresh_codegraph_index` re-syncs an existing index (used by
``whygraph scan``). Both are usable standalone (e.g. from tests).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Mapping
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

_log = logging.getLogger(__name__)

_CREDENTIAL_ENV: frozenset[str] = frozenset(
    {
        "GH_TOKEN",
        "WHYGRAPH_GIT_TOKEN",
        "WHYGRAPH_GITHUB_TOKEN_FILE",
        "CLAUDE_CODE_OAUTH_TOKEN",
        # The portal database: never a secret value, but none of codegraph's business.
        "WHYGRAPH_DATABASE_URL",
        "WHYGRAPH_DATABASE_PASSWORD_FILE",
    }
)
"""Exact env names withheld from the ``codegraph`` subprocess (besides ``*_API_KEY``)."""


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
    allow_rebuild: bool = True,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Bring ``<project_root>/.codegraph/codegraph.db`` up to date.

    When the database is missing, behaves like :func:`ensure_codegraph_db`
    (full ``init -i``). When it already exists, runs ``codegraph sync -q``
    — an incremental update of just the changes since the last index,
    which is what ``whygraph scan`` wants on each run — unless the index
    was built by a CodeGraph with a different *extraction version* than
    the binary about to run: ``sync`` keeps such an index as is ("Already
    up to date"), so it is rebuilt with ``codegraph index -q`` instead.

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
    allow_rebuild : bool, optional
        When ``False``, never run the full ``codegraph index`` for a
        foreign extraction version (and skip the ``status`` check): the
        MCP read path passes this so a tool call only ever does a quick
        ``sync``. Default ``True`` (what ``whygraph scan`` wants).
    timeout : float, optional
        Seconds before a ``codegraph`` subprocess is killed and a
        :class:`CodeGraphBootstrapError` raised. Default ``None`` (no limit).
    env : Mapping[str, str], optional
        The environment to derive the subprocess's from (credentials are
        still withheld). Default :data:`os.environ`.

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

    run = {"image": image, "capture": capture, "timeout": timeout, "env": env}
    if allow_rebuild and _extraction_version_changed(
        project_root, image=image, timeout=timeout, env=env
    ):
        _run_codegraph(project_root, ["index", "-q"], **run)
    else:
        _run_codegraph(project_root, ["sync", "-q"], **run)
    return db_path


def _extraction_version_changed(
    project_root: Path,
    *,
    image: str | None,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Whether the index predates (or postdates) the binary's extractor.

    Reads ``codegraph status --json``'s ``index`` block. Anything
    unexpected - the command failing, output that is not JSON, a CodeGraph
    without those fields - means "no", so a refresh never fails for it.
    A differing CodeGraph *version* with the same extraction version does
    not count: only the extractor decides what an index contains.
    """
    try:
        out = _run_codegraph(
            project_root,
            ["status", "--json"],
            image=image,
            capture=True,
            timeout=timeout,
            env=env,
        )
        info = json.loads(out[out.index("{") :])["index"]
        built = info["builtWithExtractionVersion"]
        current = info["currentExtractionVersion"]
    except (CodeGraphBootstrapError, ValueError, KeyError, TypeError):
        return False
    if not isinstance(built, int) or not isinstance(current, int) or built == current:
        return False
    _log.info(
        "CodeGraph index was built with extraction version %s, this CodeGraph has %s"
        " - rebuilding it (codegraph index)",
        built,
        current,
    )
    return True


def _run_codegraph(
    project_root: Path,
    args: list[str],
    *,
    image: str | None,
    capture: bool = False,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
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
    timeout : float, optional
        Kill the subprocess after this many seconds. Default no limit.
    env : Mapping[str, str], optional
        Base environment (see :func:`_codegraph_env`). Default ``os.environ``.

    Returns
    -------
    str
        The captured stdout when ``capture`` is set, else ``""``.

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
        result = subprocess.run(
            cmd,
            check=True,
            cwd=cwd,
            capture_output=capture,
            text=capture,
            env=_codegraph_env(env),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CodeGraphBootstrapError(
            f"`codegraph {label}` did not finish within {timeout:g} s"
        ) from exc
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
    return (result.stdout or "") if capture else ""


def _codegraph_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return ``base`` (default ``os.environ``) minus credentials the indexer never needs.

    CodeGraph only parses source, so every ``*_API_KEY``, ``GH_TOKEN``,
    ``WHYGRAPH_GIT_TOKEN`` and ``CLAUDE_CODE_OAUTH_TOKEN`` is withheld from it (and from the ``docker``
    client of the fallback path); the scan itself may hold them for its
    own LLM / GitHub calls. The portal database variables
    (``WHYGRAPH_DATABASE_URL`` / ``_PASSWORD_FILE``) are withheld too.
    """
    source = os.environ if base is None else base
    return {
        k: v
        for k, v in source.items()
        if not k.endswith("_API_KEY") and k not in _CREDENTIAL_ENV
    }


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
