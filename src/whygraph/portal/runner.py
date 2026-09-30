"""The scan runner: ``whygraph scan`` child processes, GitHub sync, poll and catch-up.

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
* **Jobs.** ``kind=scan`` spawns the child scanner. ``kind=sync`` (GitHub
  clones) runs ``fetch_default`` + ``fast_forward`` inside the project's
  slot, then the scan in the same slot when HEAD moved (or when a merged
  request asked for a scan anyway) - so a fast-forward never moves the
  tree under a running crawl.
* **Child I/O.** stdout JSON lines go to ``<data>/runs/<id>.jsonl``, stderr
  to ``<data>/runs/<id>.log``; both pipes are drained by their own thread
  so a flood never deadlocks the child. Every injected secret value is
  replaced by its hint before a line is written. Children run in their
  own session (process group); :meth:`ScanRunner.shutdown` SIGTERMs each
  group, waits, SIGKILLs and records the runs ``interrupted``.
* **Child environment.** Only :func:`~whygraph.services.git.credentials.pass_through_env`
  from the portal's own environment, plus ``WHYGRAPH_CONFIG_JSON`` (the
  resolved config, no secrets), ``GH_TOKEN`` when the forge is on, the
  one ``<PROVIDER>_API_KEY`` the analyze model needs (analyzing runs
  only), the git credential-helper token for GitHub clones and
  ``GIT_TERMINAL_PROMPT=0``. Never a portal-env API key or token.
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
* **Poll and catch-up.** Every :data:`POLL_INTERVAL_SEC` the poller
  enqueues a ``sync`` (``trigger=poll``) for each initialized GitHub
  clone - it never fetches itself - and runs the catch-up check, which is
  also run once at start: a local project whose HEAD differs from
  ``projects.last_scanned_head`` gets a ``trigger=hook`` scan.

The test seam :data:`SCAN_CMD_ENV` (``WHYGRAPH_SCAN_CMD``) replaces the
``whygraph scan`` prefix of the child argv (``shlex.split``, so it may
carry extra flags).
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import anyio
import anyio.to_thread
from fastapi.responses import StreamingResponse
from sqlmodel import col, select

from whygraph.core.config import Config, ConfigError
from whygraph.core.safe_paths import UnsafePathError
from whygraph.services.git import GitError, Repository
from whygraph.services.git.credentials import TOKEN_ENV_VAR, git_env, pass_through_env

from .context import resolve_root, resolved_layer
from .db import data_dir, get_session
from .models import Project, ScanRun
from .paths import check_project_paths
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
"""GitHub poll + catch-up tick."""

HEARTBEAT_SEC = 15.0
"""Idle SSE heartbeat interval."""

TAIL_INTERVAL_SEC = 0.25
"""How often an SSE stream re-reads a live run's events file."""

TRIGGER_PRECEDENCE: tuple[str, ...] = (
    "hook",
    "poll",
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

CANCELLED_BY_USER: dict[str, str] = {"cancelled_by": "user"}
"""The ``summary`` of a run the user cancelled (vs. a merged or orphaned one)."""

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
        ``"local"`` or ``"github"``.
    analyze : bool
        Whether the run describes commits (only then is an LLM key passed).
    environ : Mapping[str, str], optional
        The portal environment to filter (default :data:`os.environ`).

    Returns
    -------
    tuple[dict[str, str], list[str]]
        The environment, and every secret value injected into it (the
        redaction list).
    """
    env = pass_through_env(environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["WHYGRAPH_CONFIG_JSON"] = json.dumps(layer, default=str, sort_keys=True)
    secrets: list[str] = []
    token = config.scan_token
    if token and config.scan_forge != "off":
        env["GH_TOKEN"] = token
        secrets.append(token)
    if token and source == "github":
        env[TOKEN_ENV_VAR] = token
        if token not in secrets:
            secrets.append(token)
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


def redactor(secrets: list[str]) -> Callable[[str], str]:
    """Return a function replacing each secret value by its hint (``...a1b2``)."""
    pairs = [(s, hint_for(s)) for s in sorted(set(secrets), key=len, reverse=True) if s]

    def redact(text: str) -> str:
        for value, hint in pairs:
            if value in text:
                text = text.replace(value, hint)
        return text

    return redact


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
        Seconds between poll ticks.
    heartbeat : float
        Seconds of SSE idleness before a heartbeat comment.
    sleep : callable, optional
        ``async (seconds) -> None`` the poller waits with; tests inject a
        fake clock. Defaults to :func:`anyio.sleep`.
    """

    def __init__(
        self,
        *,
        max_concurrent: int = MAX_CONCURRENT,
        poll_interval: float = POLL_INTERVAL_SEC,
        heartbeat: float = HEARTBEAT_SEC,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.poll_interval = poll_interval
        self.heartbeat = heartbeat
        self.tail_interval = TAIL_INTERVAL_SEC
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

    # ---- lifecycle ------------------------------------------------------

    async def start(self, state: PortalState) -> None:
        """Start the queue: requeue leftover ``queued`` rows, catch up, start the poller.

        Called by the lifespan after the portal DB is migrated and stale
        ``running`` rows were marked ``interrupted``.

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
            await self._requeue()
            await self.catch_up()
        except Exception:  # noqa: BLE001 -- never fail the portal start
            _log.exception("scan runner: startup recovery failed")
        tg.start_soon(self._poll_loop)

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
            await anyio.to_thread.run_sync(_mark_interrupted, leftover)
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
        self, project: BoundProject, *, principal: Principal | None
    ) -> int:
        """Queue a GitHub ``sync`` job (``trigger=sync``) and return its id."""
        return await self._request(
            project.id,
            kind="sync",
            trigger="sync",
            analyze=False,
            requested_by=principal.user_id if principal else None,
            scan_requested=False,
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

    # ---- poll + catch-up -------------------------------------------------

    async def tick(self) -> None:
        """One poll tick: a ``poll`` sync per GitHub clone, then the catch-up check."""
        projects = await anyio.to_thread.run_sync(_initialized_projects)
        for p in projects:
            if p["source"] == "github" and p["root_ok"]:
                try:
                    await self._request(
                        p["id"],
                        kind="sync",
                        trigger="poll",
                        analyze=False,
                        requested_by=None,
                        scan_requested=False,
                    )
                except ProjectBusy:
                    continue  # being removed
        await self.catch_up(projects)

    async def catch_up(self, projects: list[dict] | None = None) -> None:
        """Queue a ``trigger=hook`` scan for each local project whose HEAD moved.

        Covers commits made while the portal was down (the hook POST was
        lost). Compares ``git rev-parse HEAD`` with
        ``projects.last_scanned_head``; a project never scanned (``NULL``)
        is left to its first, explicit scan.
        """
        if projects is None:
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

    async def _poll_loop(self) -> None:
        while True:
            await self._sleep(self.poll_interval)
            try:
                await self.tick()
            except RunnerUnavailable:
                return
            except Exception:  # noqa: BLE001 -- the poller must survive
                _log.exception("scan runner: poll tick failed")

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
        finally:
            job.done.set()

    def _execute_inner(
        self, job: _Job
    ) -> tuple[str, dict[str, Any], Callable[[str], str]]:
        state = self._state
        assert state is not None
        spec = job.spec
        with get_session() as session:
            project = session.get(Project, spec.project_id)
            run = session.get(ScanRun, spec.run_id)
            if project is None or run is None:
                return "cancelled", {"error": "the project is gone"}, redactor([])
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
        env, secrets = child_env(config, layer, source=source, analyze=spec.analyze)
        redact = redactor(secrets)
        base = data_dir()
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
            if spec.kind == "sync":
                event({"type": "sync", "status": "fetching"})
                repo = Repository(root, origin_remote=config.scan_remote)
                try:
                    repo.fetch_default(env=git_env(config.scan_token))
                    if job.cancelled:
                        # Not after the fetch either: a moved HEAD with no scan
                        # would never be caught up (catch-up is local-only).
                        return "cancelled", summary, redact
                    moved = repo.fast_forward()
                except GitError as exc:
                    message = _error_chain(exc)
                    log(f"sync failed: {message}")
                    event({"type": "sync", "status": "failed", "error": message})
                    return "failed", {"error": redact(message)}, redact
                event({"type": "sync", "status": "ok", "moved": moved})
                summary["moved"] = moved
                if not moved and not spec.scan_requested:
                    return "ok", summary, redact
            if job.interrupted:
                return "interrupted", summary, redact
            if spec.kind == "sync" and (message := refused()) is not None:
                # The fast-forward may have brought the symlink in.
                summary["error"] = message
                return "failed", summary, redact

            job.head = git_head(root)
            argv = scan_argv(spec.trigger, spec.analyze)
            log(f"$ {shlex.join(argv)}")
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
                threading.Thread(target=drain_stdout, args=(proc.stdout,), daemon=True),
                threading.Thread(target=drain_stderr, args=(proc.stderr,), daemon=True),
            ]
            for reader in readers:
                reader.start()
            code = proc.wait()
            for reader in readers:
                reader.join()
            proc.stdout.close()
            proc.stderr.close()
            state.migrations.forget(
                Path(config.whygraph_db or root / ".whygraph/whygraph.db")
            )

            if job.result is not None:
                summary.update({k: v for k, v in job.result.items() if k != "type"})
            summary["exit_code"] = code
            return ("ok" if code == 0 else "failed"), summary, redact

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
            self._stream(run_id, path, max(0, offset), shutdown),
            media_type="text/event-stream",
            headers=dict(_SSE_HEADERS),
        )

    async def _stream(
        self, run_id: int, path: Path, offset: int, shutdown: anyio.Event
    ) -> AsyncIterator[str]:
        pos = offset
        last_sent = time.monotonic()
        while True:
            if shutdown.is_set():
                yield f'event: shutdown\ndata: {{"type": "shutdown", "run_id": {run_id}}}\n\n'
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
    "POLL_INTERVAL_SEC",
    "PROVIDER_KEY_ENV",
    "SCAN_CMD_ENV",
    "TRIGGER_PRECEDENCE",
    "ProjectBusy",
    "RunNotFound",
    "RunnerUnavailable",
    "ScanRunner",
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
]
