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

Neither runs CodeGraph while the repository holds a symbolic link that
CodeGraph would read and that leads out of it
(:func:`~.symlinks.escaping_symlinks`): CodeGraph follows such links on
purpose, so they raise :class:`~.exceptions.CodeGraphUnsafeSymlinkError`
instead. An index built from scratch behind that guard gets a stamp beside
it (:data:`~.paths.GUARD_STAMP_RELPATH`); an index without one may hold
rows read through a link before the guard existed, so the next refresh that
may rebuild runs a full ``codegraph index`` (:func:`index_predates_guard`).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
from collections.abc import Mapping
from pathlib import Path

from whygraph.core.safe_paths import UnsafePathError, check_inside
from whygraph.services.git.credentials import pass_through_env

from .exceptions import CodeGraphBootstrapError, CodeGraphUnsafeSymlinkError
from .paths import CODEGRAPH_DB_RELPATH, GUARD_STAMP_RELPATH
from .symlinks import escaping_symlinks

DEFAULT_CODEGRAPH_IMAGE: str = "ghcr.io/mtrdesign/whygraph:latest"
"""Default image run by the Docker fallback when no override is given.

The self-contained WhyGraph image bakes in the CodeGraph CLI, so the
fallback runs ``codegraph`` inside it. ``whygraph scan`` exposes
``--codegraph-image`` for ad-hoc overrides; tests and CI pipelines can
also pass an explicit ``image=`` to the functions here. Ignored when a
local ``codegraph`` binary is used instead.
"""

GUARD_VERSION: str = "1"
"""The content of :data:`~.paths.GUARD_STAMP_RELPATH`; bumping it rebuilds every index once."""

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
    CodeGraphUnsafeSymlinkError
        If the repository holds a symbolic link out of it that CodeGraph
        would read; CodeGraph is not run.
    """
    project_root = project_root.resolve()
    db_path = project_root / CODEGRAPH_DB_RELPATH
    if db_path.exists():
        return db_path

    _refuse_escaping_symlinks(project_root)
    _run_codegraph(project_root, ["init", "-i"], image=image, capture=capture)

    if not db_path.exists():
        raise CodeGraphBootstrapError(
            f"codegraph exited cleanly but {db_path} was not created"
            " — check codegraph output above for errors"
        )
    _write_guard_stamp(project_root)
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
    up to date"), so it is rebuilt with ``codegraph index -q`` instead. An
    index that predates the symbolic-link guard (:func:`index_predates_guard`)
    is rebuilt the same way, once.

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
        foreign extraction version or an index that predates the guard
        (and skip the ``status`` check): the MCP read path passes this so a
        tool call only ever does a quick ``sync``. Default ``True`` (what
        ``whygraph scan`` wants).
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
    CodeGraphUnsafeSymlinkError
        If the repository holds a symbolic link out of it that CodeGraph
        would read; CodeGraph is not run.
    """
    project_root = project_root.resolve()
    db_path = project_root / CODEGRAPH_DB_RELPATH
    if not db_path.exists():
        return ensure_codegraph_db(project_root, image=image, capture=capture)

    _refuse_escaping_symlinks(project_root)
    run = {"image": image, "capture": capture, "timeout": timeout, "env": env}
    if allow_rebuild and (
        _predates_guard_logged(project_root)
        or _extraction_version_changed(
            project_root, image=image, timeout=timeout, env=env
        )
    ):
        _run_codegraph(project_root, ["index", "-q"], **run)
        _write_guard_stamp(project_root)
    else:
        _run_codegraph(project_root, ["sync", "-q"], **run)
    return db_path


def index_predates_guard(project_root: Path) -> bool:
    """Whether ``<project_root>``'s index exists but was not built behind the guard.

    Such an index may hold symbols CodeGraph read through a symbolic link
    out of the repository before WhyGraph refused those links; only a full
    rebuild drops them (a ``sync`` keeps rows of unchanged files). The
    stamp is written after an ``init`` or ``index`` that the guard let run.

    Parameters
    ----------
    project_root : Path
        The repository root.

    Returns
    -------
    bool
        ``True`` when ``.codegraph/codegraph.db`` is a file and the stamp
        is missing or names another :data:`GUARD_VERSION`. ``False`` when
        there is no index, or ``.codegraph/`` is a symbolic link (refused
        elsewhere; nothing is read through it).
    """
    try:
        db_path = check_inside(project_root, CODEGRAPH_DB_RELPATH)
        stamp = check_inside(project_root, GUARD_STAMP_RELPATH)
    except UnsafePathError:
        return False
    if not db_path.is_file():
        return False
    try:
        return stamp.read_text(encoding="utf-8").strip() != GUARD_VERSION
    except (OSError, UnicodeDecodeError):
        return True


def _predates_guard_logged(project_root: Path) -> bool:
    """:func:`index_predates_guard`, logging the rebuild it causes."""
    if not index_predates_guard(project_root):
        return False
    _log.info(
        "CodeGraph index predates WhyGraph's symbolic-link guard"
        " - rebuilding it once (codegraph index)"
    )
    return True


def _refuse_escaping_symlinks(project_root: Path) -> None:
    """Raise :class:`CodeGraphUnsafeSymlinkError` before CodeGraph could read through a link."""
    paths = escaping_symlinks(project_root)
    if paths:
        raise CodeGraphUnsafeSymlinkError(paths)


def _write_guard_stamp(project_root: Path) -> None:
    """Record that the index was built from scratch behind the guard (best effort)."""
    try:
        stamp = check_inside(project_root, GUARD_STAMP_RELPATH)
        stamp.write_text(GUARD_VERSION + "\n", encoding="utf-8")
    except (UnsafePathError, OSError) as exc:
        # Without the stamp the next refresh rebuilds again: safe, only slower.
        _log.warning("could not stamp the CodeGraph index as guarded: %s", exc)


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

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=capture,
        env=_codegraph_env(env),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_group(proc)
        raise CodeGraphBootstrapError(
            f"`codegraph {label}` did not finish within {timeout:g} s"
        ) from exc
    except BaseException:
        _kill_group(proc)
        raise
    if proc.returncode != 0:
        if capture:
            tail = (stderr or stdout or "").strip()
            detail = f"\n{tail}" if tail else ""
            raise CodeGraphBootstrapError(
                f"`codegraph {label}` failed (exit {proc.returncode}){detail}"
            )
        raise CodeGraphBootstrapError(
            f"`codegraph {label}` failed (exit {proc.returncode}) — see output above"
        )
    return (stdout or "") if capture else ""


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM ``proc``'s process group, SIGKILL after 2 s, then reap it.

    ``codegraph`` is an npm shim that starts a bundled Node server; killing
    only the shim would leave the grandchild holding CodeGraph's own file
    lock. Mirrors :meth:`whygraph.portal.runner._Job.signal`.
    """
    for sig, grace in ((signal.SIGTERM, 2.0), (signal.SIGKILL, None)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:  # pragma: no cover - e.g. a foreign group
            proc.send_signal(sig)
        try:
            proc.communicate(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


_SWITCHES: dict[str, str] = {
    "DO_NOT_TRACK": "1",
    "CODEGRAPH_TELEMETRY": "0",
    "CODEGRAPH_NO_UPDATE_CHECK": "1",
    "CODEGRAPH_NO_DOWNLOAD": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
}
"""Set on every CodeGraph run: no telemetry, update check or download, and git that
reads no global config, never prompts and never takes ``.git/index.lock``."""

_HOSTED_SWITCHES: dict[str, str] = {
    "CODEGRAPH_NO_DAEMON": "1",
    "CODEGRAPH_NO_WATCH": "1",
    "CODEGRAPH_MCP_TOOLS": "explore,node,search,impact",
}
"""Only for the portal's shared ``serve --mcp`` child. Never for a run that may
``init``: under ``NO_WATCH`` CodeGraph offers to install its own git hooks."""


def _codegraph_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return ``base`` (default ``os.environ``) minus credentials, plus the run switches.

    CodeGraph only parses source, so every ``*_API_KEY``, ``GH_TOKEN``,
    ``WHYGRAPH_GIT_TOKEN`` and ``CLAUDE_CODE_OAUTH_TOKEN`` is withheld from it (and from the ``docker``
    client of the fallback path); the scan itself may hold them for its
    own LLM / GitHub calls. The portal database variables
    (``WHYGRAPH_DATABASE_URL`` / ``_PASSWORD_FILE``) are withheld too. The
    telemetry / update / download switches and the git switches are set
    (``_SWITCHES``); ``CODEGRAPH_NO_WATCH`` and ``CODEGRAPH_NO_DAEMON`` never are.
    """
    source = os.environ if base is None else base
    env = {
        k: v
        for k, v in source.items()
        if not k.endswith("_API_KEY") and k not in _CREDENTIAL_ENV
    }
    env.update(_SWITCHES)
    return env


def hosted_env(home: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the environment of the portal's shared ``codegraph serve --mcp`` child.

    Parameters
    ----------
    home : Path
        The child's ``HOME`` (``<data>/codegraph-home``).
    base : Mapping[str, str], optional
        The environment to start from. Default
        :func:`~whygraph.services.git.credentials.pass_through_env`.

    Returns
    -------
    dict[str, str]
        :func:`_codegraph_env` of ``base`` plus the watcher / daemon switches,
        the pinned tool list and ``HOME``. No ``WHYGRAPH_*`` variable and no
        debug variable.
    """
    env = _codegraph_env(pass_through_env() if base is None else base)
    env.update(_HOSTED_SWITCHES)
    env["HOME"] = str(home)
    return env


def _user_arg() -> list[str]:
    # Pass --user uid:gid so bind-mounted files come back owned by the host
    # user. Windows lacks os.getuid/getgid; on those platforms we omit the
    # flag and trust Docker Desktop's VFS to handle ownership.
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        return ["--user", f"{os.getuid()}:{os.getgid()}"]
    return []


__all__ = [
    "DEFAULT_CODEGRAPH_IMAGE",
    "GUARD_VERSION",
    "ensure_codegraph_db",
    "hosted_env",
    "index_predates_guard",
    "refresh_codegraph_index",
]
