"""The scan runner: ``whygraph scan`` child processes, GitHub sync and catch-up.

A small in-process queue (plan section 4.6), living in the single portal
process:

* **Single-flight + coalescing per project.** A project runs at most one
  job at a time. A request while a job runs (or waits) sets - or merges
  into - one *pending* job per project, as the **union of work**: the
  merged job is remote if any request is, analyzes if any request does,
  and syncs if any request did. Its ``scan_runs`` row is created by the
  **first** request, so every coalesced request gets the same ``run_id``;
  it records the highest-precedence trigger
  (``describe > manual > initial > sync > poll > hook``) and keeps the
  first explicit ``requested_by``. A project with no completed (``ok``)
  scan records every request as ``initial`` (structure-only, section 4.14).
* **Global cap** of :data:`MAX_CONCURRENT` running jobs across projects.
* **Jobs.** ``kind=scan`` spawns the child scanner. ``kind=sync`` (the
  server clone of a production GitHub project) syncs the clone inside the
  project's slot, then runs the scan in the same slot when HEAD differs
  from ``last_scanned_head`` (or when a merged request asked for a scan
  anyway) - so a checkout never moves the tree under a running crawl.
* **Production GitHub projects** (M2d-2 plan sections 0.2 #5, #6, #14,
  #23, 4.6). Every request to scan one is a ``sync`` with
  ``scan_requested`` (manual, describe and initial scans included: the
  clone is stale by definition). The sync mints a repo-scoped
  installation token in this process, re-derives ``origin`` from the
  repository id (a rename is followed), refreshes ``default_branch`` /
  ``remote_url``, fetches, refuses a default branch that tracks
  ``.whygraph/`` / ``.codegraph/``, then ``checkout -B <default>`` (a
  force-push is followed and noted as ``history_rewritten``) and
  ``remote set-head``. The child gets the token through
  ``<data>/runs/<id>.token`` (0600, written atomically) named by
  ``WHYGRAPH_GITHUB_TOKEN_FILE`` - never ``GH_TOKEN`` /
  ``WHYGRAPH_GIT_TOKEN`` - which a refresher thread rewrites
  :attr:`ScanRunner.token_refresh_margin` seconds before the token
  expires; the file is deleted when the child exits, and leftovers are
  swept at start. Losing access (a refused mint or repository lookup, a
  git ``401`` / "not found") sets ``projects.access_lost_at`` /
  ``access_lost_reason`` (audited ``project_access_lost``) and every
  request is refused with :class:`ProjectAccessLost` until a successful
  mint clears it (``project_access_restored``). A default branch that
  tracks WhyGraph's state fails the run and marks the project with reason
  ``tracked_whygraph_state``; that mark does not refuse requests (the
  next sync re-checks, and clears it once the repository is fixed).
* **Child I/O.** stdout JSON lines go to ``<data>/runs/<id>.jsonl``, stderr
  to ``<data>/runs/<id>.log``; both pipes are drained by their own thread
  so a flood never deadlocks the child. Every injected secret value is
  replaced by its hint, and anything shaped like a GitHub token by its
  prefix (:func:`~whygraph.services.git.credentials.redact_tokens`),
  before a line is written. Children run in their own session (process
  group); :meth:`ScanRunner.shutdown` SIGTERMs each group, waits,
  SIGKILLs and records the runs ``interrupted``.
* **Child environment.** Only :func:`~whygraph.services.git.credentials.pass_through_env`
  from the portal's own environment, plus ``WHYGRAPH_CONFIG_JSON`` (the
  resolved config, no secrets), ``GH_TOKEN`` (a local folder's personal
  access token) when the forge is on, the one ``<PROVIDER>_API_KEY`` the
  analyze model needs (analyzing runs only) and ``GIT_TERMINAL_PROMPT=0``;
  a GitHub project's child gets no personal access token, git with no
  global or system config, the configured GitHub URL and (production)
  the token file. Never a portal-env API key or token.
* **Path check.** Before a sync and before spawning the child scan, the
  project's DB paths go through
  :func:`~whygraph.portal.paths.check_project_paths`; a symlinked
  ``.whygraph/`` / ``.codegraph/`` / DB file fails the run with an
  ``error`` event instead of scanning.
* **Log.** ``runs/<id>.log`` (redacted like the events) is served as a
  bounded tail by :func:`log_tail` (``GET .../scans/<id>/log``).
* **Events.** The events file is the single source of the SSE stream
  (:meth:`ScanRunner.events`): replay from a byte offset, then follow,
  ``id:`` byte offsets for resume, heartbeats, a terminal ``end`` frame.
  In production the route passes an access re-check, so a stream whose
  caller lost access ends early with ``reason: "access_revoked"``.
* **Catch-up.** At start and every :data:`POLL_INTERVAL_SEC` (local mode
  only), a local project whose HEAD differs from
  ``projects.last_scanned_head`` gets a ``trigger=hook`` scan.
* **Reconcile** (production only, M2d-2 plan section 4.7). Its own loop in
  the runner's task group, started by :meth:`ScanRunner.start` but never
  awaited there: at start and every :attr:`ScanRunner.reconcile_interval`
  seconds, each initialized, already scanned GitHub project that is not
  busy is checked in turn - an access-lost one gets one
  :meth:`ScanRunner.check_access`, the others a ``git ls-remote`` of the
  default branch with a fresh token, and a ``trigger=reconcile`` sync when
  it differs from ``last_scanned_head``. It catches the pushes whose
  webhooks a down instance missed.
* **Org deletion** (M2d-2 plan section 4.8). :meth:`ScanRunner.reserve_projects`
  marks an org as being deleted - every later request for one of its
  projects, from a route or an internal caller, raises
  :class:`ProjectBusy` - waits for the requests already past that check,
  cancels the org's queued runs and its running ones (SIGTERM, as a user
  cancel), and waits for them to end; a sync still fetching makes it give
  up (:class:`ProjectBusy`) and release everything.
* **Source policy.** Every request - from a route or an internal caller -
  for a project whose source the mode does not accept
  (:func:`~whygraph.portal.policy.allowed_sources`, e.g. a local-mode
  GitHub clone of an older build) raises :class:`SourceNotAllowed`, and a
  leftover queued run of one fails without touching the repository.

**System callers.** :meth:`ScanRunner.catch_up`, :meth:`ScanRunner.reconcile`,
the webhook (:mod:`whygraph.portal.webhook`), the queued-run recovery and
the run finisher are internal: no request,
no user and no org reach them. An internal caller starts from a project
**id** read from the DB, never from a request's slug, and resolves config
and secrets through ``ContextCache.get(project_id)``, which reads that
project's own org - so a scan child only ever receives its own org's
secrets. Such runs keep ``requested_by=None``, which is how "system" is
recorded. Request-driven work enters through ``request_scan`` /
``request_sync`` with a project the request already bound and authorized
(:func:`~whygraph.portal.deps.bind_project`).

The test seam :data:`SCAN_CMD_ENV` (``WHYGRAPH_SCAN_CMD``) replaces the
``whygraph scan`` prefix of the child argv (``shlex.split``, so it may
carry extra flags).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
)
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import anyio
import anyio.to_thread
from fastapi.responses import StreamingResponse
from sqlmodel import col, select

from whygraph.core.config import Config, ConfigError
from whygraph.core.safe_paths import UnsafePathError, check_inside
from whygraph.services.git import GitError, InvalidRepoUrlError, Repository
from whygraph.services.git.credentials import (
    GITHUB_URL_ENV,
    TOKEN_FILE_ENV,
    git_env,
    github_git_host,
    pass_through_env,
    redact_tokens,
)

from .audit import audit
from .context import resolve_root, resolved_layer
from .db import data_dir, get_session
from .github_app import GitHubAccessLost, InstallationToken
from .github_auth import GitHubUnavailable
from .models import Project, ScanRun
from .paths import TRACKED_STATE_PATHS, check_project_paths
from .policy import allowed_sources
from .repos import root_status
from .secrets import hint_for

if TYPE_CHECKING:  # pragma: no cover
    from .deps import BoundProject, PortalState
    from .security import Principal

_log = logging.getLogger(__name__)

SCAN_CMD_ENV = "WHYGRAPH_SCAN_CMD"
"""Test-only override of the child's ``whygraph scan`` prefix (``shlex.split``)."""

MAX_CONCURRENT = 2
"""Jobs running at once across all projects."""

POLL_INTERVAL_SEC = 15 * 60
"""Seconds between catch-up checks (local mode)."""

RECONCILE_INTERVAL_SEC = 60 * 60
"""Seconds between reconcile passes (production)."""

LS_REMOTE_TIMEOUT_SEC = 30
"""Seconds before a reconcile's ``git ls-remote`` is killed."""

HEARTBEAT_SEC = 15.0
"""Idle SSE heartbeat interval."""

TAIL_INTERVAL_SEC = 0.25
"""How often an SSE stream re-reads a live run's events file."""

ACCESS_CHECK_SEC = 5.0
"""How often an SSE stream re-checks that its caller may still read the run."""

TRIGGER_PRECEDENCE: tuple[str, ...] = (
    "hook",
    "poll",
    "reconcile",
    "push",
    "sync",
    "initial",
    "manual",
    "describe",
)
"""Lowest to highest: a merged pending run records the highest one."""

EXPLICIT_TRIGGERS: frozenset[str] = frozenset({"manual", "describe"})
"""Scan triggers that record ``requested_by`` (a person clicked something)."""

PROVIDER_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}
"""The only provider keys a child can receive (``claude-cli`` / ``ollama`` are key-less).

A ``claude-cli`` child can receive the Claude subscription token instead
(:data:`CLAUDE_TOKEN_ENV`), and only when that is the analyze provider.
"""

CLAUDE_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
"""How a ``claude-cli`` scan child gets the subscription token."""

LOG_TAIL_BYTES = 64 * 1024
"""How much of a run's log :func:`log_tail` returns (the end of the file)."""

CANCEL_GRACE_SEC = 10.0
"""Seconds between a cancel's SIGTERM and the SIGKILL that follows."""

ORG_DELETE_WAIT_SEC = CANCEL_GRACE_SEC + 5.0
"""Seconds an org deletion waits for its runs to end before it answers ``409 busy``.

A child gets SIGTERM, then SIGKILL after :data:`CANCEL_GRACE_SEC`; a sync
whose ``git fetch`` still runs in the portal cannot be interrupted and
outlasts this wait.
"""

CANCELLED_BY_USER: dict[str, str] = {"cancelled_by": "user"}
"""The ``summary`` of a run the user cancelled (vs. a merged or orphaned one)."""

TOKEN_REFRESH_MARGIN_SEC = 10 * 60
"""Seconds before an installation token expires that a child's token file is rewritten."""

TOKEN_RETRY_SEC = 30.0
"""Seconds between token-refresh attempts while GitHub is unavailable."""

REASON_NO_ACCESS = "no_access"
"""``access_lost_reason``: GitHub refused a token or the repository lookup
(the app was uninstalled or suspended, or no longer covers the repository)."""

REASON_GIT_DENIED = "git_access_denied"
"""``access_lost_reason``: git answered ``401`` / "not found" to a fresh token."""

REASON_REPO_DELETED = "repo_deleted"
"""``access_lost_reason``: the repository was deleted on GitHub (a ``repository``
webhook); a repository restored on GitHub comes back on the next successful mint."""

REASON_TRACKED_STATE = "tracked_whygraph_state"
"""``access_lost_reason``: the default branch tracks ``.whygraph/`` / ``.codegraph/``.

Shown like an access loss, but it never refuses a request: the next sync
re-checks and clears it once the repository is fixed.
"""

_GIT_ACCESS_DENIED = re.compile(
    r"Authentication failed|[Rr]epository not found|repository '[^']*' not found"
    r"|returned error: 40[134]"
)
"""git's words for a token that cannot read the repository."""

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
_READ_CHUNK = 256 * 1024

MAX_EVENT_LINE = _READ_CHUNK - 1
"""Longest events-file line (bytes, newline excluded) the runner writes and the stream sends.

A longer child JSON line is written as an ``oversized`` placeholder
(:func:`_event_line`); a longer line already in a file is skipped by
:func:`_read_frames` with the same placeholder, so the stream never stalls.
"""


class RunnerUnavailable(RuntimeError):
    """The runner cannot take requests (not started, or shutting down) - HTTP 501."""


class RunNotFound(LookupError):
    """No such run for this project - HTTP 404."""


class ProjectBusy(RuntimeError):
    """A project removal and a scan / sync request collided - HTTP 409."""


class RunFinished(RuntimeError):
    """The run already ended, so there is nothing to cancel - HTTP 409."""


class SourceNotAllowed(RuntimeError):
    """The project's source is not accepted in this mode - HTTP 409 ``source_not_allowed``."""


class ProjectAccessLost(RuntimeError):
    """The project lost its GitHub access - HTTP 409 ``github_access_lost``.

    Attributes
    ----------
    reason : str or None
        ``projects.access_lost_reason``.
    """

    def __init__(self, reason: str | None) -> None:
        super().__init__(_access_lost_message(reason))
        self.reason = reason


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def merge_trigger(a: str, b: str) -> str:
    """Return the higher-precedence of two triggers (:data:`TRIGGER_PRECEDENCE`)."""
    return a if TRIGGER_PRECEDENCE.index(a) >= TRIGGER_PRECEDENCE.index(b) else b


def resolve_analyze(trigger: str, analyze: bool | None) -> bool:
    """Whether a request of ``trigger`` describes commits.

    Only ``manual`` (unless ``analyze`` is ``False``) and ``describe`` spend
    on the LLM; every other trigger is structure-only.
    """
    if trigger == "describe":
        return True
    if trigger == "manual":
        return analyze is not False
    return False


def scan_flags(trigger: str, analyze: bool) -> list[str]:
    """The ``whygraph scan`` flags of the section 4.6 trigger-to-flags table.

    Parameters
    ----------
    trigger : str
        The recorded trigger.
    analyze : bool
        The recorded (resolved) analyze flag.

    Returns
    -------
    list[str]
        ``hook`` -> ``--skip-analyze --no-remote``; ``initial`` / ``poll``
        / ``sync`` -> ``--skip-analyze``; ``manual`` / ``describe`` -> none
        (full scan), or ``--skip-analyze`` when ``analyze`` is off.
    """
    if trigger == "hook":
        return ["--skip-analyze", "--no-remote"]
    if trigger in EXPLICIT_TRIGGERS and analyze:
        return []
    return ["--skip-analyze"]


def scan_argv(
    trigger: str, analyze: bool, environ: Mapping[str, str] | None = None
) -> list[str]:
    """The child scanner's argv.

    Parameters
    ----------
    trigger, analyze
        As for :func:`scan_flags`.
    environ : Mapping[str, str], optional
        Where :data:`SCAN_CMD_ENV` is read (default :data:`os.environ`).

    Returns
    -------
    list[str]
        ``$WHYGRAPH_SCAN_CMD`` (``shlex.split``) or ``<python> -m whygraph
        scan``, then ``--progress json --managed-by-portal`` and the flags.
    """
    source = os.environ if environ is None else environ
    override = (source.get(SCAN_CMD_ENV) or "").strip()
    prefix = (
        shlex.split(override)
        if override
        else [sys.executable, "-m", "whygraph", "scan"]
    )
    return [
        *prefix,
        "--progress",
        "json",
        "--managed-by-portal",
        *scan_flags(trigger, analyze),
    ]


def child_env(
    config: Config,
    layer: Mapping[str, Any],
    *,
    source: str,
    analyze: bool,
    environ: Mapping[str, str] | None = None,
    token_file: Path | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Build the child scanner's environment and the secrets it carries.

    Parameters
    ----------
    config : Config
        The project's built config (holds the decrypted secrets).
    layer : Mapping
        The resolved config v2 dict **without** secrets
        (:func:`~whygraph.portal.context.resolved_layer`).
    source : str
        ``"local"`` or ``"github"``. Only a local folder's child gets the
        personal access token; a GitHub project's (production) child gets
        none, and its git reads no global or system config.
    analyze : bool
        Whether the run describes commits (only then is an LLM key passed).
    environ : Mapping[str, str], optional
        The portal environment to filter (default :data:`os.environ`).
    token_file : Path, optional
        A production GitHub project's token file, passed as
        ``WHYGRAPH_GITHUB_TOKEN_FILE`` (the token itself never enters the
        environment; the runner writes and refreshes the file).

    Returns
    -------
    tuple[dict[str, str], list[str]]
        The environment, and every secret value injected into it (the
        redaction list).
    """
    source_env = os.environ if environ is None else environ
    env = pass_through_env(source_env)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["WHYGRAPH_CONFIG_JSON"] = json.dumps(layer, default=str, sort_keys=True)
    secrets: list[str] = []
    # No personal access token for a GitHub project (M2d-2 plan section 0.2 #13).
    token = config.scan_token if source == "local" else None
    if token and config.scan_forge != "off":
        env["GH_TOKEN"] = token
        secrets.append(token)
    if source == "github":
        # A server clone: a developer's insteadOf / http.* / include must not
        # rewrite its git calls, and the helper's host follows the config.
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        if source_env.get(GITHUB_URL_ENV):
            env[GITHUB_URL_ENV] = source_env[GITHUB_URL_ENV]
        if token_file is not None:
            env[TOKEN_FILE_ENV] = str(token_file)
    if analyze:
        try:
            provider: str | None = config.model_for("analyze").provider
        except ConfigError:
            provider = None
        var = PROVIDER_KEY_ENV.get(provider or "")
        if var is not None:
            key = getattr(config.llm.section(provider), "api_key", None)  # type: ignore[arg-type]
            if key:
                env[var] = key
                secrets.append(key)
        claude = config.llm.claude_cli.oauth_token
        if provider == "claude-cli" and claude:
            # The child's config comes from WHYGRAPH_CONFIG_JSON, which never
            # holds a secret; the adapter picks the token up from its env.
            env[CLAUDE_TOKEN_ENV] = claude
            secrets.append(claude)
    return env, secrets


class Redactor:
    """Replaces each known secret value by its hint (``...a1b2``); learns new ones.

    Anything still shaped like a GitHub token - one the runner never
    injected, or a partly masked one (``gh auth status`` prints most of a
    token) - is then replaced by its prefix and ``***``
    (:func:`~whygraph.services.git.credentials.redact_tokens`).

    Parameters
    ----------
    secrets : iterable of str
        The values known from the start.
    """

    def __init__(self, secrets: list[str] | tuple[str, ...] = ()) -> None:
        self._pairs: tuple[tuple[str, str], ...] = ()
        self.learn(*secrets)

    def learn(self, *values: str) -> None:
        """Add values to redact from now on (e.g. a refreshed installation token).

        Safe to call from another thread while lines are being redacted:
        the pairs are replaced in one assignment.
        """
        known = {value for value, _ in self._pairs} | {v for v in values if v}
        self._pairs = tuple(
            (s, hint_for(s)) for s in sorted(known, key=len, reverse=True)
        )

    def __call__(self, text: str) -> str:
        for value, hint in self._pairs:
            if value in text:
                text = text.replace(value, hint)
        return redact_tokens(text)


def redactor(secrets: list[str]) -> Redactor:
    """Return a :class:`Redactor` for ``secrets`` (see there)."""
    return Redactor(secrets)


def write_token_file(path: Path, token: str) -> None:
    """Write a token file atomically, mode ``0600``.

    The token goes to a temporary file in the same directory, created with
    ``O_EXCL`` (and ``O_NOFOLLOW``) at mode ``0600``, which then replaces
    ``path`` with :func:`os.replace` - so a reader sees the old token or the
    new one, never a torn file.

    Parameters
    ----------
    path : Path
        The token file (``<data>/runs/<id>.token``).
    token : str
        The token.
    """
    tmp = path.with_name(f"{path.name}.{os.urandom(6).hex()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def sweep_token_files(base: Path) -> int:
    """Delete token files a previous process left in ``<base>/runs``.

    Parameters
    ----------
    base : Path
        The data directory.

    Returns
    -------
    int
        How many files were removed.
    """
    runs = base / "runs"
    removed = 0
    for path in {*runs.glob("*.token"), *runs.glob("*.token.*.tmp")}:
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            pass
    return removed


def git_head(root: Path) -> str | None:
    """``git rev-parse HEAD`` in ``root``, or ``None`` (unborn, missing, not git)."""
    return _git_out(root, "rev-parse", "HEAD")


def commits_behind(root: Path, since: str) -> int | None:
    """How many commits ``HEAD`` has that ``since`` has not (``None`` if unknown)."""
    out = _git_out(root, "rev-list", "--count", f"{since}..HEAD")
    return int(out) if out and out.isdigit() else None


def stale_info(root: Path, last_scanned_head: str | None) -> dict | None:
    """The ``stale`` block of a project summary, or ``None`` when up to date.

    Parameters
    ----------
    root : Path
        The mounted repository root.
    last_scanned_head : str or None
        ``projects.last_scanned_head``; ``None`` (never scanned) is not
        "stale".

    Returns
    -------
    dict or None
        ``{"commits_behind": n}`` (``n`` is ``None`` when git cannot count,
        e.g. after a history rewrite) while HEAD differs.
    """
    if not last_scanned_head:
        return None
    head = git_head(root)
    if head is None or head == last_scanned_head:
        return None
    return {"commits_behind": commits_behind(root, last_scanned_head)}


def _git_out(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            env=pass_through_env(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@dataclass
class _Pending:
    """One project's pending (queued) job - the union of the merged requests."""

    run_id: int
    project_id: int
    kind: str
    trigger: str
    analyze: bool
    requested_by: int | None
    scan_requested: bool

    def merge(
        self,
        *,
        kind: str,
        trigger: str,
        analyze: bool,
        requested_by: int | None,
        scan_requested: bool,
    ) -> None:
        if kind == "sync":
            self.kind = "sync"
        self.trigger = merge_trigger(self.trigger, trigger)
        self.analyze = self.analyze or analyze
        if self.requested_by is None:
            self.requested_by = requested_by
        self.scan_requested = self.scan_requested or scan_requested


@dataclass
class _Job:
    """A running job: the pending snapshot plus its child process."""

    spec: _Pending
    proc: subprocess.Popen | None = None
    interrupted: bool = False
    cancelled: bool = False
    head: str | None = None
    scanned: bool = False
    result: dict | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    done: threading.Event = field(default_factory=threading.Event)

    def signal(self, sig: int) -> None:
        """Send ``sig`` to the child's process group (no-op without a live child)."""
        with self.lock:
            self.interrupted = True
            proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:  # pragma: no cover - e.g. a foreign group
            proc.send_signal(sig)

    def alive(self) -> bool:
        proc = self.proc
        return proc is not None and proc.poll() is None

    def cancel(self) -> None:
        """Mark the job cancelled by the user and SIGTERM its child.

        Before the child exists (a sync still fetching), the flag makes
        :meth:`ScanRunner._execute_inner` stop before it starts one.
        """
        with self.lock:
            self.cancelled = True
        self.signal(signal.SIGTERM)


@dataclass(frozen=True)
class _GitHubProject:
    """What a job needs to know about a production GitHub project (read at job start)."""

    repo_id: int
    installation_id: int
    default_branch: str | None
    last_scanned_head: str | None


@dataclass(frozen=True)
class _ReconcileTarget:
    """A production GitHub project the reconcile looks at (read at the pass's start)."""

    id: int
    root: Path
    root_ok: bool
    lost: bool
    github: _GitHubProject


class _SyncFailed(Exception):
    """A production sync failed; the message is shown (redacted) in the run."""


class _TokenRefresher:
    """Rewrites a running child's token file before its token expires.

    A daemon thread: it sleeps until ``margin`` seconds before the current
    token's expiry, mints a new one through ``mint``, teaches it to the
    redactor, then replaces the file (:func:`write_token_file`). A refused
    mint (``GitHubAccessLost`` - ``mint`` has marked the project) stops it,
    and the child fails on its next call; GitHub being unavailable is
    retried every ``retry`` seconds. After :meth:`stop` returns, the file is
    never written again, so the caller may delete it.

    Parameters
    ----------
    mint : callable
        Returns a fresh :class:`~whygraph.portal.github_app.InstallationToken`.
    path : Path
        The token file.
    expires_at : float
        Epoch seconds when the token in the file expires.
    margin, retry : float
        Seconds before expiry to refresh; seconds between failed attempts.
    redact : Redactor
        Learns every new token before it is written.
    """

    def __init__(
        self,
        *,
        mint: Callable[[], InstallationToken],
        path: Path,
        expires_at: float,
        margin: float,
        retry: float,
        redact: Redactor,
    ) -> None:
        self._mint = mint
        self._path = path
        self._expires_at = expires_at
        self._margin = margin
        self._retry = retry
        self._redact = redact
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, name=f"token-refresh-{path.stem}", daemon=True
        )

    def start(self) -> None:
        """Start the thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop refreshing; no write happens after this returns."""
        with self._lock:
            self._stop.set()

    def _run(self) -> None:
        expires = self._expires_at
        while not self._stop.wait(max(0.0, expires - self._margin - time.time())):
            try:
                token = self._mint()
            except GitHubAccessLost:
                _log.warning(
                    "scan runner: GitHub access lost; %s not refreshed", self._path.name
                )
                return
            except Exception:  # noqa: BLE001 -- e.g. GitHub unavailable: retry
                _log.warning(
                    "scan runner: could not refresh %s; retrying", self._path.name
                )
                if self._stop.wait(self._retry):
                    return
                continue
            self._redact.learn(token.token)
            with self._lock:
                if self._stop.is_set():
                    return
                write_token_file(self._path, token.token)
            expires = token.expires_at


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


class ScanRunner:
    """The portal's scan queue (see the module docstring).

    Parameters
    ----------
    max_concurrent : int
        Global cap on running jobs.
    poll_interval : float
        Seconds between catch-up checks (local mode).
    heartbeat : float
        Seconds of SSE idleness before a heartbeat comment.
    sleep : callable, optional
        ``async (seconds) -> None`` the poller and the reconcile wait with;
        tests inject a fake clock. Defaults to :func:`anyio.sleep`.
    reconcile_interval : float
        Seconds between reconcile passes (production).

    Attributes
    ----------
    token_refresh_margin : float
        Seconds before an installation token expires that a running
        child's token file is rewritten (:data:`TOKEN_REFRESH_MARGIN_SEC`;
        tests shorten it).
    token_retry : float
        Seconds between refresh attempts while GitHub is unavailable.
    org_delete_wait : float
        Seconds :meth:`reserve_projects` waits for an org's runs to end
        (:data:`ORG_DELETE_WAIT_SEC`; tests shorten it).
    """

    def __init__(
        self,
        *,
        max_concurrent: int = MAX_CONCURRENT,
        poll_interval: float = POLL_INTERVAL_SEC,
        heartbeat: float = HEARTBEAT_SEC,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        reconcile_interval: float = RECONCILE_INTERVAL_SEC,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.poll_interval = poll_interval
        self.reconcile_interval = reconcile_interval
        self.heartbeat = heartbeat
        self.tail_interval = TAIL_INTERVAL_SEC
        self.token_refresh_margin: float = TOKEN_REFRESH_MARGIN_SEC
        self.token_retry: float = TOKEN_RETRY_SEC
        self.org_delete_wait: float = ORG_DELETE_WAIT_SEC
        self._sleep = sleep or anyio.sleep
        self._state: PortalState | None = None
        self._tg: Any = None
        self._lock: anyio.Lock | None = None
        self._limiter: anyio.CapacityLimiter | None = None
        self._pending: dict[int, _Pending] = {}
        self._running: dict[int, _Job] = {}
        self._live: set[int] = set()
        self._stopping = False
        # Removal vs request claims (threading: removal runs in a worker thread).
        self._claims = threading.Lock()
        self._removing: set[int] = set()
        self._requesting: dict[int, int] = {}
        # Org deletion: orgs being deleted, and requests past the org check.
        self._deleting_orgs: set[int] = set()
        self._org_requests: dict[int, int] = {}

    # ---- lifecycle ------------------------------------------------------

    async def start(self, state: PortalState) -> None:
        """Start the queue: sweep token files, requeue ``queued`` rows, catch up, poll.

        Called by the lifespan after the portal DB is migrated and stale
        ``running`` rows were marked ``interrupted``. Local mode then polls
        (:meth:`catch_up`); production starts the reconcile loop
        (:meth:`reconcile`), which runs on its own and never delays the
        start.

        Parameters
        ----------
        state : PortalState
            The portal state (context cache, migrations, data dir).
        """
        self._state = state
        self._lock = anyio.Lock()
        self._limiter = anyio.CapacityLimiter(max(1, self.max_concurrent))
        self._stopping = False
        tg = anyio.create_task_group()
        await tg.__aenter__()
        self._tg = tg
        try:
            # A previous process's children are gone: so is any use of their tokens.
            await anyio.to_thread.run_sync(sweep_token_files, data_dir())
            await self._requeue()
            await self.catch_up()
        except Exception:  # noqa: BLE001 -- never fail the portal start
            _log.exception("scan runner: startup recovery failed")
        if self._local_mode():
            tg.start_soon(self._poll_loop)
        elif self._mode() == "production":
            tg.start_soon(self._reconcile_loop)

    async def shutdown(self, *, grace: float = 5.0) -> None:
        """Stop every child and record its run ``interrupted``.

        SIGTERM to each child's process group, up to ``grace`` seconds for
        them to exit, SIGKILL for the rest. Queued rows stay ``queued`` and
        are requeued by the next start.

        Parameters
        ----------
        grace : float
            Seconds between SIGTERM and SIGKILL.
        """
        if self._tg is None:
            return
        self._stopping = True
        jobs = list(self._running.values())
        for job in jobs:
            job.signal(signal.SIGTERM)
        deadline = time.monotonic() + grace
        while any(j.alive() for j in jobs) and time.monotonic() < deadline:
            await anyio.sleep(0.05)
        for job in jobs:
            if job.alive():
                job.signal(signal.SIGKILL)
        # Let the job threads record the outcome; a thread stuck in a
        # network git call is abandoned and its run marked here.
        deadline = time.monotonic() + 2.0
        while any(not j.done.is_set() for j in jobs) and time.monotonic() < deadline:
            await anyio.sleep(0.05)
        leftover = [j.spec.run_id for j in jobs if not j.done.is_set()]
        if leftover:
            try:
                await anyio.to_thread.run_sync(_mark_interrupted, leftover)
            except Exception:  # noqa: BLE001 -- e.g. the database is gone
                # The rows stay "running"; the next start marks them interrupted.
                _log.exception(
                    "scan runner: could not mark runs %s interrupted", leftover
                )
        tg, self._tg = self._tg, None
        tg.cancel_scope.cancel()
        await tg.__aexit__(None, None, None)

    def is_busy(self, project_id: int) -> bool:
        """Return whether a scan or sync for the project is queued or running.

        Parameters
        ----------
        project_id : int
            ``projects.id``.

        Returns
        -------
        bool
            Read from ``scan_runs``.
        """
        with get_session() as session:
            row = session.exec(
                select(ScanRun.id)
                .where(ScanRun.project_id == project_id)
                .where(col(ScanRun.status).in_(("queued", "running")))
            ).first()
        return row is not None

    @contextmanager
    def reserve_removal(self, project_id: int) -> Iterator[None]:
        """Hold off scan / sync requests for a project while it is removed.

        Atomic with :meth:`_request`: a request already in flight, or a
        queued / running run, makes the reservation fail; while it is held,
        every new request for the project raises :class:`ProjectBusy`.

        Parameters
        ----------
        project_id : int
            ``projects.id``.

        Yields
        ------
        None
            The reservation is released when the block exits.

        Raises
        ------
        ProjectBusy
            If a scan or sync is queued, running or being requested, or
            another removal holds the reservation.
        """
        with self._claims:
            if project_id in self._removing or self._requesting.get(project_id):
                raise ProjectBusy(
                    "a scan or sync is queued or running for this project"
                )
            self._removing.add(project_id)
        try:
            if self.is_busy(project_id):
                raise ProjectBusy(
                    "a scan or sync is queued or running for this project"
                )
            yield
        finally:
            with self._claims:
                self._removing.discard(project_id)

    @asynccontextmanager
    async def reserve_projects(self, org_id: int) -> AsyncIterator[frozenset[int]]:
        """Stop and hold off every run of an org's projects while the org is deleted.

        In order (M2d-2 plan section 4.8 step 1): mark the org as being
        deleted, so every later run request for one of its projects - from
        a route, the webhook or the reconcile - raises
        :class:`ProjectBusy`; reserve its projects as a removal does; wait
        for the requests already past the org check; drop its queued runs
        (recorded ``cancelled``) and cancel its running ones (SIGTERM, then
        SIGKILL after :data:`CANCEL_GRACE_SEC`); wait for them to end.

        Parameters
        ----------
        org_id : int
            ``organizations.id``.

        Yields
        ------
        frozenset of int
            The org's project ids at the time of the reservation (a project
            imported later can never get a run either). Everything is
            released when the block exits, however it exits.

        Raises
        ------
        ProjectBusy
            The org is already being deleted, one of its projects is being
            removed, or a run did not end within :attr:`org_delete_wait`
            seconds (a sync whose fetch cannot be interrupted).
        """
        with self._claims:
            if org_id in self._deleting_orgs:
                raise ProjectBusy("the organization is already being deleted")
            self._deleting_orgs.add(org_id)
        reserved: set[int] = set()
        try:
            ids = await anyio.to_thread.run_sync(_org_project_ids, org_id)
            with self._claims:
                if ids & self._removing:
                    raise ProjectBusy(
                        "a project of this organization is being removed; try again"
                    )
                self._removing |= ids
                reserved = set(ids)
            deadline = time.monotonic() + self.org_delete_wait
            while not self._org_idle(org_id, ids):
                if time.monotonic() >= deadline:
                    raise ProjectBusy("a scan is being requested; try again")
                await anyio.sleep(0.02)
            jobs = await self._cancel_projects(ids)
            while not all(job.done.is_set() for job in jobs):
                if time.monotonic() >= deadline:
                    raise ProjectBusy("a sync is finishing; try again in a minute")
                await anyio.sleep(0.05)
            yield frozenset(ids)
        finally:
            with self._claims:
                self._deleting_orgs.discard(org_id)
                self._removing -= reserved

    def _org_idle(self, org_id: int, ids: set[int]) -> bool:
        """Whether no run request for the org (or one of ``ids``) is in flight."""
        with self._claims:
            return not self._org_requests.get(org_id) and not any(
                self._requesting.get(i) for i in ids
            )

    async def _cancel_projects(self, ids: set[int]) -> list[_Job]:
        """Drop the queued runs of ``ids`` and cancel their running ones; return those jobs."""
        if self._lock is None:  # never started: nothing queued or running
            return []
        async with self._lock:
            # Nothing awaits between the pops and the cancels, so _dispatch
            # cannot start one of these runs in between.
            dropped = [self._pending.pop(i) for i in ids if i in self._pending]
            jobs = [self._running[i] for i in ids if i in self._running]
            for job in jobs:
                job.cancel()
                if self._tg is not None:
                    self._tg.start_soon(self._kill_after_grace, job)
            for pending in dropped:
                await anyio.to_thread.run_sync(_mark_cancelled, pending.run_id)
                self._live.discard(pending.run_id)
        return jobs

    @contextmanager
    def _claim_org(self, org_id: int | None) -> Iterator[None]:
        """Count a request past the org check, or refuse it while its org is deleted."""
        if org_id is None:
            yield
            return
        with self._claims:
            if org_id in self._deleting_orgs:
                raise ProjectBusy("the organization is being deleted")
            self._org_requests[org_id] = self._org_requests.get(org_id, 0) + 1
        try:
            yield
        finally:
            with self._claims:
                left = self._org_requests.pop(org_id) - 1
                if left:
                    self._org_requests[org_id] = left

    @contextmanager
    def _claim_request(self, project_id: int) -> Iterator[None]:
        """Mark a request in flight for the project, or refuse it during a removal."""
        with self._claims:
            if project_id in self._removing:
                raise ProjectBusy("the project is being removed")
            self._requesting[project_id] = self._requesting.get(project_id, 0) + 1
        try:
            yield
        finally:
            with self._claims:
                left = self._requesting.pop(project_id) - 1
                if left:
                    self._requesting[project_id] = left

    # ---- requests -------------------------------------------------------

    async def request_scan(
        self,
        project: BoundProject,
        *,
        trigger: str | None,
        analyze: bool | None,
        principal: Principal | None,
    ) -> int:
        """Queue (or coalesce into) a scan and return its ``scan_runs.id``.

        Parameters
        ----------
        project : BoundProject
            The (initialized) project.
        trigger : str or None
            ``manual`` (default), ``hook`` or ``describe``.
        analyze : bool or None
            ``False`` turns a ``manual`` scan structure-only.
        principal : Principal or None
            Recorded as ``requested_by`` for ``manual`` / ``describe`` only.

        Returns
        -------
        int
            The pending run's id (the same for every coalesced request).

        Raises
        ------
        RunnerUnavailable, ProjectBusy, SourceNotAllowed, ProjectAccessLost
            As for :meth:`request_sync`.

        Notes
        -----
        For a production GitHub project the request becomes a ``sync`` that
        scans afterwards (the clone is fetched first).
        """
        requested = trigger or "manual"
        user = (
            principal.user_id if principal and requested in EXPLICIT_TRIGGERS else None
        )
        return await self._request(
            project.id,
            kind="scan",
            trigger=requested,
            analyze=resolve_analyze(requested, analyze),
            requested_by=user,
            scan_requested=True,
        )

    async def request_sync(
        self,
        project_id: int,
        *,
        trigger: str = "sync",
        scan_requested: bool = False,
        principal: Principal | None = None,
    ) -> int:
        """Queue (or coalesce into) a sync of a production GitHub project.

        The sync fetches and checks out the default branch, then scans when
        HEAD differs from ``last_scanned_head`` (or when ``scan_requested``,
        or a merged request asked for a scan). Internal callers (webhook,
        reconcile) pass a project id read from the DB.

        Parameters
        ----------
        project_id : int
            ``projects.id``.
        trigger : str
            ``sync`` (default), ``push`` or ``reconcile`` - a member of
            :data:`TRIGGER_PRECEDENCE`.
        scan_requested : bool
            Scan even when HEAD did not move.
        principal : Principal or None
            Recorded as ``requested_by``.

        Returns
        -------
        int
            The pending run's id (the same for every coalesced request).

        Raises
        ------
        RunnerUnavailable
            The runner is not running.
        ProjectBusy
            The project is being removed.
        SourceNotAllowed
            The project is not a GitHub project of a production portal (or
            its source is not accepted in this mode).
        ProjectAccessLost
            The project lost its GitHub access (``access_lost_at`` set,
            other than for :data:`REASON_TRACKED_STATE`).
        """
        if trigger not in TRIGGER_PRECEDENCE:
            raise ValueError(f"unknown trigger {trigger!r}")
        return await self._request(
            project_id,
            kind="sync",
            trigger=trigger,
            analyze=False,
            requested_by=principal.user_id if principal else None,
            scan_requested=scan_requested,
        )

    async def _request(
        self,
        project_id: int,
        *,
        kind: str,
        trigger: str,
        analyze: bool,
        requested_by: int | None,
        scan_requested: bool,
    ) -> int:
        if self._tg is None or self._lock is None or self._stopping:
            raise RunnerUnavailable("the scan runner is not running")
        with self._claim_request(project_id):
            gate = await anyio.to_thread.run_sync(_project_gate, project_id)
            with self._claim_org(None if gate is None else gate[3]):
                if gate is not None:
                    source, lost, reason, _org_id = gate
                    if source not in allowed_sources(self._mode()):
                        raise SourceNotAllowed(_unsupported_source(source))
                    if lost and reason != REASON_TRACKED_STATE:
                        raise ProjectAccessLost(reason)
                    github = self._mode() == "production" and source == "github"
                    if kind == "sync" and not github:
                        raise SourceNotAllowed("only a production GitHub project syncs")
                    if github and kind == "scan":
                        # Scan now fetches first (M2d-2 plan section 0.2 #23).
                        kind, scan_requested = "sync", True
                return await self._request_claimed(
                    project_id,
                    kind=kind,
                    trigger=trigger,
                    analyze=analyze,
                    requested_by=requested_by,
                    scan_requested=scan_requested,
                )

    async def _request_claimed(
        self,
        project_id: int,
        *,
        kind: str,
        trigger: str,
        analyze: bool,
        requested_by: int | None,
        scan_requested: bool,
    ) -> int:
        assert self._lock is not None
        async with self._lock:
            if not await anyio.to_thread.run_sync(_has_ok_scan, project_id):
                trigger, analyze = "initial", False
            pending = self._pending.get(project_id)
            if pending is None:
                run_id = await anyio.to_thread.run_sync(
                    _insert_run, project_id, kind, trigger, analyze, requested_by
                )
                self._pending[project_id] = _Pending(
                    run_id=run_id,
                    project_id=project_id,
                    kind=kind,
                    trigger=trigger,
                    analyze=analyze,
                    requested_by=requested_by,
                    scan_requested=scan_requested,
                )
                self._live.add(run_id)
            else:
                pending.merge(
                    kind=kind,
                    trigger=trigger,
                    analyze=analyze,
                    requested_by=requested_by,
                    scan_requested=scan_requested,
                )
                run_id = pending.run_id
                await anyio.to_thread.run_sync(_update_queued, pending)
            self._dispatch()
            return run_id

    async def cancel(self, project_id: int, run_id: int) -> str:
        """Cancel a queued or running run of a project.

        A queued run is dropped from the queue and recorded ``cancelled``.
        A running one gets SIGTERM (SIGKILL after :data:`CANCEL_GRACE_SEC`)
        and is recorded ``cancelled`` when its child exits. Either way the
        summary is :data:`CANCELLED_BY_USER`, and the next request for the
        project queues a fresh run.

        Parameters
        ----------
        project_id : int
            ``projects.id`` - a run of another project is "not found".
        run_id : int
            ``scan_runs.id``.

        Returns
        -------
        str
            ``"queued"`` or ``"running"`` - what the run was.

        Raises
        ------
        RunnerUnavailable
            If the runner is not running.
        RunNotFound
            If there is no such run for the project.
        RunFinished
            If the run already ended.
        """
        if self._tg is None or self._lock is None or self._stopping:
            raise RunnerUnavailable("the scan runner is not running")
        async with self._lock:
            if self._tg is None or self._stopping:  # shut down while we waited
                raise RunnerUnavailable("the scan runner is not running")
            pending = self._pending.get(project_id)
            if pending is not None and pending.run_id == run_id:
                del self._pending[project_id]
                # The row first: a stream that sees the run leave `_live`
                # reads its final status next, which must not be "queued".
                await anyio.to_thread.run_sync(_mark_cancelled, run_id)
                self._live.discard(run_id)
                return "queued"
            job = self._running.get(project_id)
            if job is not None and job.spec.run_id == run_id:
                job.cancel()
                self._tg.start_soon(self._kill_after_grace, job)
                return "running"
        status, _summary = await anyio.to_thread.run_sync(
            _project_run_status, project_id, run_id
        )
        if status is None:
            raise RunNotFound(run_id)
        raise RunFinished(f"run {run_id} already ended ({status})")

    async def _kill_after_grace(self, job: _Job) -> None:
        await anyio.sleep(CANCEL_GRACE_SEC)
        if job.alive():
            job.signal(signal.SIGKILL)

    # ---- catch-up --------------------------------------------------------

    async def catch_up(self) -> None:
        """Queue a ``trigger=hook`` scan for each local project whose HEAD moved.

        Covers commits made while the portal was down (the hook POST was
        lost). Compares ``git rev-parse HEAD`` with
        ``projects.last_scanned_head``; a project never scanned (``NULL``)
        is left to its first, explicit scan. Local mode only (so also skipped
        at a production start).
        """
        if not self._local_mode():
            return
        projects = await anyio.to_thread.run_sync(_initialized_projects)
        for p in projects:
            if p["source"] != "local" or not p["root_ok"] or not p["last_scanned_head"]:
                continue
            head = await anyio.to_thread.run_sync(git_head, p["root"])
            if head is not None and head != p["last_scanned_head"]:
                try:
                    await self._request(
                        p["id"],
                        kind="scan",
                        trigger="hook",
                        analyze=False,
                        requested_by=None,
                        scan_requested=True,
                    )
                except ProjectBusy:
                    continue  # being removed

    def _mode(self) -> str | None:
        """The portal's mode (``None`` before :meth:`start`)."""
        return None if self._state is None else self._state.mode

    def _local_mode(self) -> bool:
        """Whether the portal runs in local mode (system work is local-only)."""
        return self._mode() == "local"

    async def _poll_loop(self) -> None:
        while True:
            await self._sleep(self.poll_interval)
            try:
                await self.catch_up()
            except RunnerUnavailable:
                return
            except Exception:  # noqa: BLE001 -- the poller must survive
                _log.exception("scan runner: catch-up failed")

    # ---- reconcile (production) ------------------------------------------

    async def reconcile(self) -> None:
        """Queue a ``trigger=reconcile`` sync for each GitHub project whose remote moved.

        The safety net under the webhook (M2d-2 plan section 4.7): a push
        whose delivery a down or unreachable instance missed is caught here.
        Sequentially, for each initialized production GitHub project that is
        not busy and not being removed:

        * an access-lost one (other than :data:`REASON_TRACKED_STATE`) gets
          one :meth:`check_access` - a success clears the state, so a
          restored repository or re-added installation comes back without
          a webhook;
        * the others, once scanned, get ``git ls-remote origin
          refs/heads/<default>`` with a freshly minted token (at most
          :data:`LS_REMOTE_TIMEOUT_SEC` seconds), and a sync when the answer
          differs from ``last_scanned_head``. A never-scanned project is
          left to its first, explicit scan, as :meth:`catch_up` does.

        A refused mint marks the project access-lost (:meth:`_mint`); GitHub
        or git being unavailable skips the project until the next pass.
        Production only, and only with the GitHub App configured.

        Raises
        ------
        RunnerUnavailable
            The runner stopped during the pass.
        """
        state = self._state
        if state is None or state.mode != "production" or state.github_app is None:
            return
        targets = await anyio.to_thread.run_sync(_reconcile_targets)
        for target in targets:
            if self._tg is None or self._stopping:
                raise RunnerUnavailable("the scan runner is not running")
            if self._is_removing(target.id):
                continue  # a project removal (or its org's deletion) holds it
            if await anyio.to_thread.run_sync(self.is_busy, target.id):
                continue
            try:
                if target.lost:
                    await anyio.to_thread.run_sync(
                        self.check_access, target.id, abandon_on_cancel=True
                    )
                    continue
                if not target.root_ok or target.github.last_scanned_head is None:
                    continue
                head = await anyio.to_thread.run_sync(
                    self._remote_head, target, abandon_on_cancel=True
                )
            except GitHubAccessLost:
                continue  # marked by _mint
            except (GitHubUnavailable, GitError) as exc:
                _log.warning(
                    "scan runner: reconcile skipped project %s: %s", target.id, exc
                )
                continue
            if head is None or head == target.github.last_scanned_head:
                continue
            try:
                await self.request_sync(target.id, trigger="reconcile")
            except (ProjectBusy, ProjectAccessLost, SourceNotAllowed):
                continue

    def _remote_head(self, target: _ReconcileTarget) -> str | None:
        """Mint a token and ``ls-remote`` the default branch (worker thread)."""
        token = self._mint(target.id, target.github, force=True)
        branch = target.github.default_branch
        assert branch is not None  # _reconcile_targets keeps only these
        return Repository(target.root).remote_branch_head(
            branch, env=git_env(token.token), timeout=LS_REMOTE_TIMEOUT_SEC
        )

    def _is_removing(self, project_id: int) -> bool:
        """Whether a removal holds the project's reservation."""
        with self._claims:
            return project_id in self._removing

    async def _reconcile_loop(self) -> None:
        """Reconcile at start, then every :attr:`reconcile_interval` seconds."""
        while True:
            try:
                await self.reconcile()
            except RunnerUnavailable:
                return
            except Exception:  # noqa: BLE001 -- the loop must survive
                _log.exception("scan runner: reconcile failed")
            await self._sleep(self.reconcile_interval)

    # ---- dispatch + execution -------------------------------------------

    def _dispatch(self) -> None:
        """Start pending jobs (oldest first) while under the cap. Loop-thread only."""
        while not self._stopping and self._tg is not None:
            if len(self._running) >= self.max_concurrent:
                return
            waiting = [
                p for pid, p in self._pending.items() if pid not in self._running
            ]
            if not waiting:
                return
            spec = min(waiting, key=lambda p: p.run_id)
            del self._pending[spec.project_id]
            job = _Job(spec=spec)
            self._running[spec.project_id] = job
            self._tg.start_soon(self._run_job, job)

    async def _run_job(self, job: _Job) -> None:
        try:
            await anyio.to_thread.run_sync(
                self._execute, job, abandon_on_cancel=True, limiter=self._limiter
            )
        except Exception:  # noqa: BLE001 -- a job must never kill the task group
            _log.exception("scan runner: job %s crashed", job.spec.run_id)
        finally:
            if self._running.get(job.spec.project_id) is job:
                del self._running[job.spec.project_id]
            self._live.discard(job.spec.run_id)
            self._dispatch()

    def _execute(self, job: _Job) -> None:
        """Run one job to completion and record its outcome (worker thread)."""
        summary: dict[str, Any] = {}
        redact = redactor([])
        try:
            status, summary, redact = self._execute_inner(job)
        except Exception as exc:  # noqa: BLE001 -- recorded as a failed run
            _log.exception("scan runner: run %s failed", job.spec.run_id)
            status, summary = "failed", {"error": redact(str(exc))}
        # A run that finished cleanly before the cancel reached it stays ok:
        # its writes all landed, and recording it cancelled would re-scan.
        if job.cancelled and status != "ok":
            status, summary = "cancelled", {**summary, **CANCELLED_BY_USER}
        elif job.interrupted and status != "cancelled":
            status = "interrupted"
        try:
            _finish_run(job, status, summary)
        except Exception:  # noqa: BLE001 -- e.g. the database is gone
            # The row stays "running"; the next start marks it interrupted.
            _log.exception(
                "scan runner: could not record run %s as %s", job.spec.run_id, status
            )
        finally:
            job.done.set()

    def _execute_inner(
        self, job: _Job
    ) -> tuple[str, dict[str, Any], Callable[[str], str]]:
        state = self._state
        assert state is not None
        spec = job.spec
        github: _GitHubProject | None = None
        with get_session() as session:
            project = session.get(Project, spec.project_id)
            run = session.get(ScanRun, spec.run_id)
            if project is None or run is None:
                return "cancelled", {"error": "the project is gone"}, redactor([])
            if project.source not in allowed_sources(state.mode):
                # A run queued before the source was dropped (requeued at start).
                return (
                    "failed",
                    {"error": _unsupported_source(project.source)},
                    redactor([]),
                )
            if state.mode == "production" and project.source == "github":
                reason = project.access_lost_reason
                if (
                    project.access_lost_at is not None
                    and reason != REASON_TRACKED_STATE
                ):
                    # Queued before the access was lost (or requeued at start).
                    return (
                        "failed",
                        {"error": _access_lost_message(reason)},
                        redactor([]),
                    )
                if (
                    project.github_repo_id is None
                    or project.github_installation_id is None
                ):
                    return (
                        "failed",
                        {
                            "error": "this project was not imported through the GitHub App"
                        },
                        redactor([]),
                    )
                github = _GitHubProject(
                    repo_id=project.github_repo_id,
                    installation_id=project.github_installation_id,
                    default_branch=project.default_branch,
                    last_scanned_head=project.last_scanned_head,
                )
            layer = resolved_layer(session, project)
            root = resolve_root(project)
            source = project.source
            run.status = "running"
            run.started_at = _now()
            run.kind = spec.kind
            run.trigger = spec.trigger
            run.analyze = spec.analyze
            session.add(run)
            events_rel, log_rel = run.events_path, run.log_path
        ctx = state.contexts.get(spec.project_id)
        config = ctx.config
        base = data_dir()
        token_path = base / "runs" / f"{spec.run_id}.token" if github else None
        env, secrets = child_env(
            config, layer, source=source, analyze=spec.analyze, token_file=token_path
        )
        redact = redactor(secrets)
        events_path, log_path = base / str(events_rel), base / str(log_rel)
        events_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        with open(events_path, "ab") as events_fh, open(log_path, "ab") as log_fh:

            def event(obj: dict) -> None:
                line = redact(json.dumps(obj)) + "\n"
                with job.write_lock:
                    events_fh.write(line.encode("utf-8"))
                    events_fh.flush()

            def log(text: str) -> None:
                with job.write_lock:
                    log_fh.write((redact(text) + "\n").encode("utf-8"))
                    log_fh.flush()

            def refused() -> str | None:
                """Why the job must not touch ``root`` (logged + evented), or ``None``."""
                if root_status(root) != "ok":
                    message = f"project root {root} is missing or not a git repository"
                else:
                    try:
                        check_project_paths(root)
                        return None
                    except UnsafePathError as exc:
                        message = (
                            f"refusing to scan: {exc} (WhyGraph never follows a "
                            "symbolic link out of the repository)"
                        )
                log(message)
                event({"type": "error", "message": message})
                return message

            if (message := refused()) is not None:
                return "failed", {"error": message}, redact

            summary: dict[str, Any] = {}
            token: InstallationToken | None = None
            if spec.kind == "sync":
                if github is None:
                    message = "only a production GitHub project syncs"
                    log(message)
                    event({"type": "error", "message": message})
                    return "failed", {"error": message}, redact
                event({"type": "sync", "status": "fetching"})
                try:
                    synced = self._github_sync(job, github, root, redact)
                except _SyncFailed as exc:
                    message = redact(str(exc))
                    log(f"sync failed: {message}")
                    event({"type": "sync", "status": "failed", "error": message})
                    return "failed", {"error": message}, redact
                if synced is None:
                    # Cancelled after the fetch: the tree did not move.
                    return "cancelled", summary, redact
                token, synced_summary = synced
                summary.update(synced_summary)
                event({"type": "sync", "status": "ok", **synced_summary})
                if (
                    not spec.scan_requested
                    and git_head(root) == github.last_scanned_head
                ):
                    return "ok", summary, redact
            if job.interrupted:
                return "interrupted", summary, redact
            if spec.kind == "sync" and (message := refused()) is not None:
                # The checkout may have brought the symlink in.
                summary["error"] = message
                return "failed", summary, redact

            job.head = git_head(root)
            argv = scan_argv(spec.trigger, spec.analyze)
            log(f"$ {shlex.join(argv)}")
            refresher: _TokenRefresher | None = None
            try:
                if github is not None:
                    assert token_path is not None
                    try:
                        if token is None:
                            token = self._mint(spec.project_id, github)
                    except (GitHubAccessLost, GitHubUnavailable) as exc:
                        message = f"could not get a GitHub token: {exc}"
                        log(message)
                        event({"type": "error", "message": message})
                        return "failed", {**summary, "error": message}, redact
                    redact.learn(token.token)
                    write_token_file(token_path, token.token)
                proc = subprocess.Popen(
                    argv,
                    cwd=root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                with job.lock:
                    job.proc = proc
                    interrupted = job.interrupted
                if interrupted:
                    job.signal(signal.SIGTERM)
                job.scanned = True
                if github is not None and token is not None:
                    assert token_path is not None
                    gh = github
                    refresher = _TokenRefresher(
                        mint=lambda: self._mint(spec.project_id, gh, force=True),
                        path=token_path,
                        expires_at=token.expires_at,
                        margin=self.token_refresh_margin,
                        retry=self.token_retry,
                        redact=redact,
                    )
                    refresher.start()

                def drain_stdout(stream: IO[bytes]) -> None:
                    for raw in iter(stream.readline, b""):
                        text = redact(raw.decode("utf-8", "replace").rstrip("\r\n"))
                        try:
                            obj = json.loads(text)
                        except ValueError:
                            obj = None
                        if isinstance(obj, dict):
                            with job.write_lock:
                                events_fh.write(_event_line(text, obj))
                                events_fh.flush()
                            if obj.get("type") == "result":
                                job.result = obj
                        elif text:
                            log(f"[stdout] {text}")

                def drain_stderr(stream: IO[bytes]) -> None:
                    for raw in iter(stream.readline, b""):
                        log(raw.decode("utf-8", "replace").rstrip("\r\n"))

                assert proc.stdout is not None and proc.stderr is not None
                readers = [
                    threading.Thread(
                        target=drain_stdout, args=(proc.stdout,), daemon=True
                    ),
                    threading.Thread(
                        target=drain_stderr, args=(proc.stderr,), daemon=True
                    ),
                ]
                for reader in readers:
                    reader.start()
                code = proc.wait()
                for reader in readers:
                    reader.join()
                proc.stdout.close()
                proc.stderr.close()
            finally:
                if refresher is not None:
                    refresher.stop()
                if token_path is not None:
                    token_path.unlink(missing_ok=True)
            state.migrations.forget(
                Path(config.whygraph_db or root / ".whygraph/whygraph.db")
            )

            if job.result is not None:
                summary.update({k: v for k, v in job.result.items() if k != "type"})
            summary["exit_code"] = code
            return ("ok" if code == 0 else "failed"), summary, redact

    # ---- production GitHub projects --------------------------------------

    def check_access(self, project_id: int) -> bool:
        """Mint the project's installation token once, to learn whether access returned.

        Blocking (call it from a worker thread). A successful mint clears
        an access loss (``project_access_restored``) - except a
        :data:`REASON_TRACKED_STATE` mark, which only a passing sync
        clears -; a refused one marks it. Not a run request: it works on an
        access-lost project, which is how the reconcile retries one.

        Parameters
        ----------
        project_id : int
            ``projects.id`` of a production GitHub project.

        Returns
        -------
        bool
            ``True`` when GitHub minted a token; ``False`` when it refused,
            or the project is not a GitHub App project.

        Raises
        ------
        GitHubUnavailable
            GitHub could not answer, or the GitHub App is not configured.
        """
        github = _github_project(project_id)
        if github is None:
            return False
        try:
            # Forced: a cached token outlives an uninstall, so it proves nothing.
            self._mint(project_id, github, force=True)
        except GitHubAccessLost:
            return False
        return True

    def _mint(
        self, project_id: int, github: _GitHubProject, *, force: bool = False
    ) -> InstallationToken:
        """Mint (or reuse) the project's installation token; track access loss.

        A refused mint marks the project access-lost (:data:`REASON_NO_ACCESS`);
        a successful one clears an access loss, except the
        :data:`REASON_TRACKED_STATE` mark (only a passing sync clears that).

        Raises
        ------
        GitHubAccessLost
            GitHub refused the token.
        GitHubUnavailable
            GitHub could not answer, or the GitHub App is not configured.
        """
        app = None if self._state is None else self._state.github_app
        if app is None:
            raise GitHubUnavailable("the GitHub App is not configured")
        try:
            token = app.installation_token(
                github.installation_id, github.repo_id, force=force
            )
        except GitHubAccessLost:
            _mark_access_lost(project_id, REASON_NO_ACCESS)
            raise
        _clear_access_lost(project_id, keep=REASON_TRACKED_STATE)
        return token

    def _github_sync(
        self, job: _Job, github: _GitHubProject, root: Path, redact: Redactor
    ) -> tuple[InstallationToken, dict[str, Any]] | None:
        """Sync a production clone (worker thread): see the module docstring.

        Returns
        -------
        tuple or None
            The token it minted and the summary fields (``moved``, plus
            ``history_rewritten`` / ``default_branch`` when they apply);
            ``None`` when the job was cancelled after the fetch.

        Raises
        ------
        _SyncFailed
            Any failure; access loss and tracked state also mark the project.
        """
        project_id = job.spec.project_id
        try:
            # Forced: a cached token may have died with an uninstall, and the
            # child that follows wants a token with its whole hour ahead.
            token = self._mint(project_id, github, force=True)
            redact.learn(token.token)
            assert self._state is not None and self._state.github_app is not None
            app = self._state.github_app
            info = app.installation_repository(token.token, github.repo_id)
        except GitHubAccessLost as exc:
            _mark_access_lost(project_id, REASON_NO_ACCESS)
            raise _SyncFailed(f"GitHub access lost: {exc}") from exc
        except GitHubUnavailable as exc:
            raise _SyncFailed(f"GitHub is unavailable: {exc}") from exc
        if info.id != github.repo_id:
            raise _SyncFailed("GitHub answered for another repository")
        branch = info.default_branch
        repo = Repository(root)
        try:
            # origin is re-derived from the repository id on every sync, so a
            # renamed or transferred repo is followed and a freed name never
            # fetches someone else's history (M2d-2 plan section 0.2 #1).
            repo.set_remote_url(f"{github_git_host().url}/{info.full_name}.git")
            _update_github_repo(
                project_id,
                remote_url=f"{app.config.web_url}/{info.full_name}",
                default_branch=branch,
            )
            try:
                repo.fetch_default(env=git_env(token.token))
            except GitError as exc:
                if _GIT_ACCESS_DENIED.search(_error_chain(exc)):
                    _mark_access_lost(project_id, REASON_GIT_DENIED)
                raise
            if job.cancelled:
                return None
            target = repo.remote_branch_commit(branch)
            if target is None:
                raise _SyncFailed(f"the fetch brought no {branch!r} branch")
            tracked = repo.tracked_paths(target, *TRACKED_STATE_PATHS)
            if tracked:
                _mark_access_lost(project_id, REASON_TRACKED_STATE)
                raise _SyncFailed(
                    f"{info.full_name} tracks WhyGraph's own state "
                    f"({', '.join(tracked[:5])}); remove .whygraph/ and "
                    ".codegraph/ from the repository"
                )
            before = git_head(root)
            repo.checkout_reset(branch, target)
            repo.set_remote_head(branch)
        except (GitError, InvalidRepoUrlError) as exc:
            raise _SyncFailed(_error_chain(exc)) from exc
        _clear_access_lost(project_id)
        summary: dict[str, Any] = {"moved": before != target}
        if before and before != target and repo.is_ancestor(before, target) is False:
            summary["history_rewritten"] = True
        if branch != github.default_branch:
            summary["default_branch"] = branch
        return token, summary

    async def _requeue(self) -> None:
        """Turn ``queued`` rows left by a previous process back into pending jobs."""
        specs = await anyio.to_thread.run_sync(_recover_queued)
        async with self._lock:  # type: ignore[union-attr]
            for spec in specs:
                self._pending[spec.project_id] = spec
                self._live.add(spec.run_id)
            self._dispatch()

    # ---- events SSE -----------------------------------------------------

    async def events(
        self,
        project: BoundProject,
        run_id: int,
        *,
        shutdown: anyio.Event,
        offset: int = 0,
        still_allowed: Callable[[], bool] | None = None,
    ) -> StreamingResponse:
        """Return the SSE response streaming a run's events.

        Parameters
        ----------
        project : BoundProject
            The run's project.
        run_id : int
            ``scan_runs.id``.
        shutdown : anyio.Event
            The lifespan's shutdown event; the stream ends when it is set.
        offset : int
            Byte offset to resume from (the last ``id:`` the client saw,
            from ``Last-Event-ID``).
        still_allowed : callable, optional
            A blocking check that the caller may still read the run, run
            in a worker thread at most every :data:`ACCESS_CHECK_SEC`; when
            it returns ``False`` the stream sends a terminal ``end`` frame
            with ``reason: "access_revoked"`` and closes. ``None`` (local
            mode) never re-checks.

        Returns
        -------
        StreamingResponse
            ``text/event-stream`` with ``Cache-Control: no-cache`` and
            ``X-Accel-Buffering: no``.

        Raises
        ------
        RunNotFound
            If the run does not exist or belongs to another project.
        """
        events_rel = await anyio.to_thread.run_sync(
            _run_events_path, project.id, run_id
        )
        if events_rel is None:
            raise RunNotFound(run_id)
        path = data_dir() / events_rel
        return StreamingResponse(
            self._stream(run_id, path, max(0, offset), shutdown, still_allowed),
            media_type="text/event-stream",
            headers=dict(_SSE_HEADERS),
        )

    async def _stream(
        self,
        run_id: int,
        path: Path,
        offset: int,
        shutdown: anyio.Event,
        still_allowed: Callable[[], bool] | None = None,
    ) -> AsyncIterator[str]:
        pos = offset
        last_sent = last_checked = time.monotonic()
        while True:
            if shutdown.is_set():
                yield f'event: shutdown\ndata: {{"type": "shutdown", "run_id": {run_id}}}\n\n'
                return
            if (
                still_allowed is not None
                and time.monotonic() - last_checked >= ACCESS_CHECK_SEC
            ):
                allowed = await anyio.to_thread.run_sync(still_allowed)
                last_checked = time.monotonic()
                if not allowed:
                    end = {
                        "type": "end",
                        "run_id": run_id,
                        "status": None,
                        "summary": None,
                        "reason": "access_revoked",
                    }
                    yield f"id: {pos}\nevent: end\ndata: {json.dumps(end)}\n\n"
                    return
            finished = run_id not in self._live
            frames, pos = _read_frames(path, pos)
            for frame in frames:
                yield frame
            if frames:
                last_sent = time.monotonic()
                continue
            if finished:
                status, summary = await anyio.to_thread.run_sync(_run_status, run_id)
                end = {
                    "type": "end",
                    "run_id": run_id,
                    "status": status,
                    "summary": summary,
                }
                yield f"id: {pos}\nevent: end\ndata: {json.dumps(end)}\n\n"
                return
            with anyio.move_on_after(self.tail_interval):
                await shutdown.wait()
            if time.monotonic() - last_sent >= self.heartbeat:
                yield ": heartbeat\n\n"
                last_sent = time.monotonic()


# ---------------------------------------------------------------------------
# Blocking DB / file helpers (worker threads)
# ---------------------------------------------------------------------------


def _error_chain(exc: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(parts) < 4:
        parts.append(str(current))
        current = current.__cause__
    return ": ".join(p for p in parts if p)


def _project_gate(project_id: int) -> tuple[str, bool, str | None, int] | None:
    """``(source, access lost?, access_lost_reason, org_id)``, or ``None`` for a missing project."""
    with get_session() as session:
        project = session.get(Project, project_id)
        if project is None:
            return None
        return (
            project.source,
            project.access_lost_at is not None,
            project.access_lost_reason,
            project.org_id,
        )


def _org_project_ids(org_id: int) -> set[int]:
    """The ids of an org's projects."""
    with get_session() as session:
        return set(session.exec(select(Project.id).where(Project.org_id == org_id)))


def run_files(project_ids: Iterable[int]) -> list[tuple[int, str | None, str | None]]:
    """The run files of some projects' runs, read before their rows go.

    Parameters
    ----------
    project_ids : iterable of int
        ``projects.id`` values.

    Returns
    -------
    list of tuple
        ``(run id, events_path, log_path)`` per ``scan_runs`` row (the
        paths relative to the data dir), for :func:`remove_run_files`.
    """
    ids = list(project_ids)
    if not ids:
        return []
    with get_session() as session:
        rows = session.exec(
            select(ScanRun.id, ScanRun.events_path, ScanRun.log_path).where(
                col(ScanRun.project_id).in_(ids)
            )
        ).all()
    return [(int(run_id), events, log) for run_id, events, log in rows]


def remove_run_files(
    base: Path, runs: Iterable[tuple[int, str | None, str | None]]
) -> int:
    """Delete removed runs' events, log and token files (M2d-2 plan section 0.2 #17).

    Each path must stay inside ``<base>/runs`` with no symlink on the way
    (:func:`~whygraph.core.safe_paths.check_inside`); one that does not is
    left and logged. Missing files are fine.

    Parameters
    ----------
    base : Path
        The data directory.
    runs : iterable of tuple
        ``(run id, events_path, log_path)`` as :func:`run_files` returns.

    Returns
    -------
    int
        How many files were removed.
    """
    runs_dir = base / "runs"
    removed = 0
    for run_id, events, log in runs:
        for rel in (events, log, f"runs/{run_id}.token"):
            if not rel:
                continue
            try:
                path = check_inside(runs_dir, base / rel)
            except UnsafePathError as exc:
                _log.warning("scan runner: left run file %s: %s", rel, exc)
                continue
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass
            except OSError as exc:
                _log.warning("scan runner: could not remove %s: %s", path, exc)
    return removed


def _github_project(project_id: int) -> _GitHubProject | None:
    """A GitHub App project's identity, or ``None`` (missing, or not imported through the app)."""
    with get_session() as session:
        project = session.get(Project, project_id)
        if (
            project is None
            or project.source != "github"
            or project.github_repo_id is None
            or project.github_installation_id is None
        ):
            return None
        return _GitHubProject(
            repo_id=project.github_repo_id,
            installation_id=project.github_installation_id,
            default_branch=project.default_branch,
            last_scanned_head=project.last_scanned_head,
        )


def _access_lost_message(reason: str | None) -> str:
    """The refusal message for an access-lost project."""
    if reason == REASON_GIT_DENIED:
        return "GitHub refused git access to this repository - reconnect it on GitHub"
    if reason == REASON_REPO_DELETED:
        return "the repository was deleted on GitHub"
    return (
        "the WhyGraph GitHub App cannot reach this repository any more - "
        "reconnect it on GitHub"
    )


def _mark_access_lost(project_id: int, reason: str) -> None:
    """Set ``access_lost_at`` (first time only) and ``access_lost_reason``; audit a change."""
    with get_session() as session:
        project = session.get(Project, project_id)
        if project is None:
            return
        if project.access_lost_at is not None and (
            project.access_lost_reason == reason
            # A refused mint is what a deleted repository looks like: keep saying so.
            or (
                project.access_lost_reason == REASON_REPO_DELETED
                and reason == REASON_NO_ACCESS
            )
        ):
            return
        project.access_lost_at = project.access_lost_at or _now()
        project.access_lost_reason = reason
        session.add(project)
        org_id, slug = project.org_id, project.slug
    audit("project_access_lost", {}, org_id=org_id, project=slug, reason=reason)


def _clear_access_lost(project_id: int, *, keep: str | None = None) -> bool:
    """Clear an access loss (unless its reason is ``keep``); audit it. Whether it cleared."""
    with get_session() as session:
        project = session.get(Project, project_id)
        if project is None or project.access_lost_at is None:
            return False
        if keep is not None and project.access_lost_reason == keep:
            return False
        reason = project.access_lost_reason
        project.access_lost_at = None
        project.access_lost_reason = None
        session.add(project)
        org_id, slug = project.org_id, project.slug
    audit("project_access_restored", {}, org_id=org_id, project=slug, reason=reason)
    return True


def _update_github_repo(
    project_id: int, *, remote_url: str, default_branch: str
) -> None:
    """Record a production project's current display URL and default branch."""
    with get_session() as session:
        project = session.get(Project, project_id)
        if project is None:
            return
        if (project.remote_url, project.default_branch) == (remote_url, default_branch):
            return
        project.remote_url = remote_url
        project.default_branch = default_branch
        session.add(project)


def _unsupported_source(source: str) -> str:
    """The refusal message for a project whose ``source`` the mode does not accept."""
    if source == "github":
        return (
            "GitHub projects are no longer supported in local mode - remove the project"
        )
    return f"{source} projects are not supported in this mode"


def _has_ok_scan(project_id: int) -> bool:
    with get_session() as session:
        project = session.get(Project, project_id)
        return project is not None and project.last_scan_at is not None


def _insert_run(
    project_id: int, kind: str, trigger: str, analyze: bool, requested_by: int | None
) -> int:
    with get_session() as session:
        run = ScanRun(
            project_id=project_id,
            kind=kind,
            trigger=trigger,
            analyze=analyze,
            requested_by=requested_by,
            status="queued",
        )
        session.add(run)
        session.flush()
        assert run.id is not None
        run.events_path = f"runs/{run.id}.jsonl"
        run.log_path = f"runs/{run.id}.log"
        session.add(run)
        return run.id


def _queued_summary(pending: _Pending) -> str | None:
    """The ``summary`` a queued row carries: whether a sync must also scan.

    A scan merged into a pending ``sync`` is not visible from ``kind`` /
    ``trigger`` (a never-scanned project's trigger is ``initial``), so it is
    persisted for :func:`_recover_queued`. ``_finish_run`` overwrites it.
    """
    if pending.kind == "sync" and pending.scan_requested:
        return json.dumps({"scan_requested": True})
    return None


def _queued_scan_requested(run: ScanRun) -> bool:
    """Whether a queued row asked for a scan: the persisted flag, else inferred."""
    try:
        summary = json.loads(run.summary) if run.summary else None
    except ValueError:
        summary = None
    if isinstance(summary, dict) and "scan_requested" in summary:
        return bool(summary["scan_requested"])
    # Rows queued before the flag was persisted.
    return run.kind == "scan" or run.trigger in ("manual", "describe", "hook")


def _update_queued(pending: _Pending) -> None:
    with get_session() as session:
        run = session.get(ScanRun, pending.run_id)
        if run is None:
            return
        run.kind = pending.kind
        run.trigger = pending.trigger
        run.analyze = pending.analyze
        run.requested_by = pending.requested_by
        run.summary = _queued_summary(pending)
        session.add(run)


def _finish_run(job: _Job, status: str, summary: dict[str, Any]) -> None:
    now = _now()
    with get_session() as session:
        run = session.get(ScanRun, job.spec.run_id)
        if run is None:
            return
        run.status = status
        run.finished_at = now
        run.summary = json.dumps(summary, default=str) if summary else None
        session.add(run)
        if status == "ok" and job.scanned:
            project = session.get(Project, job.spec.project_id)
            if project is not None:
                project.last_scan_at = now
                if job.head:
                    project.last_scanned_head = job.head
                session.add(project)


def _mark_cancelled(run_id: int) -> None:
    """Record a still-queued run as cancelled by the user."""
    with get_session() as session:
        run = session.get(ScanRun, run_id)
        if run is not None and run.status == "queued":
            run.status = "cancelled"
            run.finished_at = _now()
            run.summary = json.dumps(CANCELLED_BY_USER)
            session.add(run)


def _project_run_status(project_id: int, run_id: int) -> tuple[str | None, Any]:
    """``(status, summary)`` of a run of this project, ``(None, None)`` if none."""
    with get_session() as session:
        run = session.get(ScanRun, run_id)
        if run is None or run.project_id != project_id:
            return None, None
        return run.status, run.summary


def _mark_interrupted(run_ids: list[int]) -> None:
    with get_session() as session:
        for run_id in run_ids:
            run = session.get(ScanRun, run_id)
            if run is not None and run.status in ("queued", "running"):
                run.status = "interrupted"
                run.finished_at = _now()
                session.add(run)


def _recover_queued() -> list[_Pending]:
    """Requeue ``queued`` rows (one pending job per project); cancel the rest."""
    specs: dict[int, _Pending] = {}
    kept: dict[int, ScanRun] = {}
    with get_session() as session:
        rows = session.exec(
            select(ScanRun).where(ScanRun.status == "queued").order_by(col(ScanRun.id))
        ).all()
        for run in rows:
            assert run.id is not None
            project = session.get(Project, run.project_id)
            gone = (
                project is None
                or project.initialized_at is None
                or root_status(resolve_root(project)) != "ok"
            )
            existing = specs.get(run.project_id)
            if gone or existing is not None:
                run.status = "cancelled"
                run.finished_at = _now()
                if existing is not None and not gone:
                    existing.merge(
                        kind=run.kind,
                        trigger=run.trigger,
                        analyze=run.analyze,
                        requested_by=run.requested_by,
                        scan_requested=_queued_scan_requested(run),
                    )
                    run.summary = json.dumps({"merged_into": existing.run_id})
                session.add(run)
                continue
            specs[run.project_id] = _Pending(
                run_id=run.id,
                project_id=run.project_id,
                kind=run.kind,
                trigger=run.trigger,
                analyze=run.analyze,
                requested_by=run.requested_by,
                scan_requested=_queued_scan_requested(run),
            )
            kept[run.project_id] = run
        # Fold merged rows into the kept one, in this same session (a second
        # session writing while this one holds the write lock would block).
        for project_id, spec in specs.items():
            row = kept[project_id]
            row.kind, row.trigger = spec.kind, spec.trigger
            row.analyze, row.requested_by = spec.analyze, spec.requested_by
            row.summary = _queued_summary(spec)
            session.add(row)
    return list(specs.values())


def _initialized_projects() -> list[dict]:
    with get_session() as session:
        rows = session.exec(
            select(Project).where(col(Project.initialized_at).is_not(None))
        ).all()
        out = []
        for project in rows:
            root = resolve_root(project)
            out.append(
                {
                    "id": project.id,
                    "source": project.source,
                    "root": root,
                    "root_ok": root_status(root) == "ok",
                    "last_scanned_head": project.last_scanned_head,
                }
            )
        return out


def _reconcile_targets() -> list[_ReconcileTarget]:
    """Every initialized GitHub App project with a default branch, oldest first."""
    with get_session() as session:
        rows = session.exec(
            select(Project)
            .where(Project.source == "github")
            .where(col(Project.initialized_at).is_not(None))
            .where(col(Project.github_repo_id).is_not(None))
            .where(col(Project.github_installation_id).is_not(None))
            .where(col(Project.default_branch).is_not(None))
            .order_by(col(Project.id))
        ).all()
        out = []
        for project in rows:
            assert project.id is not None
            assert project.github_repo_id is not None
            assert project.github_installation_id is not None
            root = resolve_root(project)
            out.append(
                _ReconcileTarget(
                    id=project.id,
                    root=root,
                    root_ok=root_status(root) == "ok",
                    lost=project.access_lost_at is not None
                    and project.access_lost_reason != REASON_TRACKED_STATE,
                    github=_GitHubProject(
                        repo_id=project.github_repo_id,
                        installation_id=project.github_installation_id,
                        default_branch=project.default_branch,
                        last_scanned_head=project.last_scanned_head,
                    ),
                )
            )
        return out


def _run_events_path(project_id: int, run_id: int) -> str | None:
    with get_session() as session:
        run = session.get(ScanRun, run_id)
        if run is None or run.project_id != project_id:
            return None
        return run.events_path or f"runs/{run_id}.jsonl"


def log_tail(project_id: int, run_id: int, limit: int = LOG_TAIL_BYTES) -> dict:
    """Return the last ``limit`` bytes of a run's ``runs/<id>.log`` (blocking).

    The log is redacted when it is written (every line goes through the
    run's :func:`redactor`), so the tail is served as is.

    Parameters
    ----------
    project_id : int
        The project the run must belong to.
    run_id : int
        ``scan_runs.id``.
    limit : int
        Maximum bytes returned; default :data:`LOG_TAIL_BYTES`.

    Returns
    -------
    dict
        ``{"run_id", "text", "size", "truncated"}``: ``size`` is the whole
        file's size in bytes, ``truncated`` whether ``text`` starts after
        the file's start (the cut-off first line is then dropped). A run
        without a log yet (queued) has ``text == ""`` and ``size == 0``.

    Raises
    ------
    RunNotFound
        If the run does not exist or belongs to another project.
    """
    with get_session() as session:
        run = session.get(ScanRun, run_id)
        if run is None or run.project_id != project_id:
            raise RunNotFound(run_id)
        log_rel = run.log_path or f"runs/{run_id}.log"
    try:
        with open(data_dir() / log_rel, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            start = max(0, size - limit)
            fh.seek(start)
            data = fh.read(limit)
    except FileNotFoundError:
        return {"run_id": run_id, "text": "", "size": 0, "truncated": False}
    truncated = start > 0
    if truncated:
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline >= 0 else b""
    return {
        "run_id": run_id,
        "text": data.decode("utf-8", "replace"),
        "size": size,
        "truncated": truncated,
    }


def _run_status(run_id: int) -> tuple[str | None, Any]:
    with get_session() as session:
        run = session.get(ScanRun, run_id)
        if run is None:
            return None, None
        summary = json.loads(run.summary) if run.summary else None
        return run.status, summary


def _oversized(size: int, original_type: Any = None) -> dict:
    return {"type": "oversized", "original_type": original_type, "bytes": size}


def _event_line(text: str, obj: dict) -> bytes:
    """Encode one child JSON line for the events file, placeholder when too long."""
    data = text.encode("utf-8")
    if len(data) > MAX_EVENT_LINE:
        kind = obj.get("type")
        data = json.dumps(
            _oversized(len(data), kind if isinstance(kind, str) else None)
        ).encode("utf-8")
    return data + b"\n"


def _read_frames(path: Path, pos: int) -> tuple[list[str], int]:
    """Read the complete lines after byte ``pos`` as SSE frames (``id:`` = end offset).

    A complete line longer than a read chunk becomes one ``oversized``
    placeholder frame and is skipped; an incomplete one (no newline yet)
    waits for the next call.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(pos)
            data = fh.read(_READ_CHUNK)
            end = data.rfind(b"\n")
            if end < 0 and len(data) == _READ_CHUNK:
                # One line fills the chunk: find where it ends, without keeping it.
                scanned = len(data)
                while chunk := fh.read(_READ_CHUNK):
                    newline = chunk.find(b"\n")
                    if newline >= 0:
                        size = scanned + newline
                        pos += size + 1
                        frame = json.dumps(_oversized(size))
                        return [f"id: {pos}\ndata: {frame}\n\n"], pos
                    scanned += len(chunk)
                return [], pos
    except FileNotFoundError:
        return [], pos
    if end < 0:
        return [], pos
    frames = []
    for line in data[: end + 1].split(b"\n")[:-1]:
        pos += len(line) + 1
        if line.strip():
            frames.append(f"id: {pos}\ndata: {line.decode('utf-8', 'replace')}\n\n")
    return frames, pos


__all__ = [
    "EXPLICIT_TRIGGERS",
    "LOG_TAIL_BYTES",
    "MAX_CONCURRENT",
    "MAX_EVENT_LINE",
    "LS_REMOTE_TIMEOUT_SEC",
    "POLL_INTERVAL_SEC",
    "PROVIDER_KEY_ENV",
    "REASON_GIT_DENIED",
    "REASON_NO_ACCESS",
    "REASON_REPO_DELETED",
    "RECONCILE_INTERVAL_SEC",
    "REASON_TRACKED_STATE",
    "SCAN_CMD_ENV",
    "TOKEN_REFRESH_MARGIN_SEC",
    "TRIGGER_PRECEDENCE",
    "ProjectAccessLost",
    "ProjectBusy",
    "Redactor",
    "RunNotFound",
    "RunnerUnavailable",
    "ScanRunner",
    "SourceNotAllowed",
    "child_env",
    "commits_behind",
    "git_head",
    "log_tail",
    "merge_trigger",
    "redactor",
    "resolve_analyze",
    "scan_argv",
    "scan_flags",
    "stale_info",
    "sweep_token_files",
    "write_token_file",
]
