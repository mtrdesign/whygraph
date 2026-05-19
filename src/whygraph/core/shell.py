"""Subprocess wrapper with structured errors and DEBUG-level tracing.

Exposes :class:`Shell`, a small configurable wrapper around
:func:`subprocess.run` that captures stdout/stderr, raises
:class:`ShellError` on non-zero exit, and emits every invocation,
returncode, and (truncated) captured output to the
``whygraph.core.shell`` logger at DEBUG. Service-layer clients
(``GitClient``, ``GitHubClient``, …) hold a :class:`Shell` instance and
go through its :meth:`Shell.run` rather than calling
:func:`subprocess.run` directly, so a single place controls timeouts,
environment, and observability for every external command in whygraph.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, overload

from whygraph.core.shell_command import ShellCommand

T = TypeVar("T")

_log = logging.getLogger(__name__)

_MAX_LOG_CHARS = 2000
"""Per-stream cap on captured-output bytes logged at DEBUG."""


def _log_stream(name: str, text: str) -> None:
    """Emit a captured stream at DEBUG with a per-stream char cap.

    Empty streams are skipped entirely. Streams shorter than
    :data:`_MAX_LOG_CHARS` are logged verbatim; longer ones are
    truncated, with a trailer indicating how many chars were dropped.
    """
    if not text:
        return
    n = len(text)
    if n > _MAX_LOG_CHARS:
        _log.debug(
            "%s (%d chars, truncated to %d):\n%s\n[...%d more chars]",
            name,
            n,
            _MAX_LOG_CHARS,
            text[:_MAX_LOG_CHARS],
            n - _MAX_LOG_CHARS,
        )
    else:
        _log.debug("%s (%d chars):\n%s", name, n, text)


class ShellError(RuntimeError):
    """Raised when a subprocess exits non-zero under ``check=True``.

    Carries the full invocation context so callers (and log inspectors)
    can reconstruct what ran and why it failed without rerunning it.

    Parameters
    ----------
    cmd : Sequence[str]
        The command argv as passed to :meth:`Shell.run`.
    returncode : int
        The exit code returned by the subprocess.
    stdout : str
        The captured standard output (may be empty).
    stderr : str
        The captured standard error (may be empty).

    Attributes
    ----------
    cmd : list[str]
        Argv list — always a list, even if a tuple was supplied.
    returncode : int
        Subprocess exit code.
    stdout : str
        Captured standard output.
    stderr : str
        Captured standard error.
    """

    def __init__(
        self,
        cmd: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"{shlex.join(self.cmd)!r} exited {returncode}: "
            f"{stderr.strip() or stdout.strip()}"
        )


@dataclass
class Shell:
    """Configurable subprocess runner.

    Holds the defaults shared across every invocation (timeout,
    environment). Per-call kwargs override these for one-off needs.

    Parameters
    ----------
    timeout : int, optional
        Default per-invocation timeout in seconds (default ``30``).
    env : Mapping[str, str], optional
        Default environment for the subprocess. ``None`` (default)
        inherits from the current process. Per-call ``env=`` overrides
        this.

    Attributes
    ----------
    timeout : int
        Default per-invocation timeout in seconds.
    env : Mapping[str, str] or None
        Default subprocess environment, or ``None`` to inherit.
    """

    timeout: int = 30
    env: Mapping[str, str] | None = None

    @overload
    def run(
        self,
        cmd: ShellCommand[T],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> T: ...

    @overload
    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]: ...

    def run(
        self,
        cmd: Sequence[str] | ShellCommand[T],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str] | T:
        """Run a subprocess, capture its output, and optionally raise on failure.

        Accepts two call shapes:

        * **argv** (``Sequence[str]``) — runs the command and returns the
          raw :class:`~subprocess.CompletedProcess`.
        * **:class:`ShellCommand`** — calls :meth:`ShellCommand.argv` to
          build the argv, runs it, then delegates to
          :meth:`ShellCommand.parse` and returns the typed result. Useful
          when the same argv-plus-parser pair is reused across services.

        Every call is logged to ``whygraph.core.shell`` at DEBUG with
        the shell-quoted command, effective cwd, returncode, elapsed
        wall-clock, and (truncated) captured stdout/stderr. Captured
        output is also returned in full on the :class:`CompletedProcess`
        — the truncation only applies to what lands in the log.

        Parameters
        ----------
        cmd : Sequence[str] or ShellCommand[T]
            Either an argv list/tuple, or a :class:`ShellCommand`
            instance that knows how to build its own argv and parse the
            result.
        cwd : Path, optional
            Working directory. Defaults to the inherited CWD.
        timeout : int, optional
            Override the instance's :attr:`timeout` for this call.
        check : bool, optional
            If ``True`` (default), a non-zero exit raises
            :class:`ShellError` before any :meth:`ShellCommand.parse`
            runs. Set to ``False`` to inspect (or parse) the result
            regardless of exit code.
        env : Mapping[str, str], optional
            Override the instance's :attr:`env` for this call.

        Returns
        -------
        subprocess.CompletedProcess[str] or T
            For an argv call, the completed process with captured
            ``stdout`` and ``stderr``. For a :class:`ShellCommand` call,
            the value returned by :meth:`ShellCommand.parse`.

        Raises
        ------
        ShellError
            If ``check`` is ``True`` and the subprocess exits non-zero.
        subprocess.TimeoutExpired
            If the subprocess does not finish within ``timeout`` seconds.
        """
        if isinstance(cmd, ShellCommand):
            result = self._run_argv(
                cmd.argv(),
                cwd=cwd,
                timeout=timeout,
                check=check,
                env=env,
            )
            return cmd.parse(result)

        return self._run_argv(
            cmd,
            cwd=cwd,
            timeout=timeout,
            check=check,
            env=env,
        )

    def _run_argv(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path | None,
        timeout: int | None,
        check: bool,
        env: Mapping[str, str] | None,
    ) -> subprocess.CompletedProcess[str]:
        """Execute ``cmd`` and return the captured :class:`CompletedProcess`.

        Shared core of the argv and :class:`ShellCommand` branches of
        :meth:`run`. Applies instance defaults for ``timeout``/``env``,
        emits the DEBUG trace, and raises :class:`ShellError` on
        non-zero exit when ``check`` is true.
        """
        effective_timeout = timeout if timeout is not None else self.timeout
        effective_env = env if env is not None else self.env

        _log.debug("$ %s (cwd=%s)", shlex.join(cmd), cwd or Path.cwd())
        start = time.monotonic()
        result = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=effective_timeout,
            env=dict(effective_env) if effective_env is not None else None,
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _log.debug("→ returncode=%d in %dms", result.returncode, elapsed_ms)
        if _log.isEnabledFor(logging.DEBUG):
            _log_stream("stdout", result.stdout)
            _log_stream("stderr", result.stderr)
        if check and result.returncode != 0:
            raise ShellError(cmd, result.returncode, result.stdout, result.stderr)
        return result

    @overload
    def run_all(
        self,
        cmds: Sequence[ShellCommand[T]],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
        check: bool = True,
        env: Mapping[str, str] | None = None,
        max_workers: int | None = None,
    ) -> list[T]: ...

    @overload
    def run_all(
        self,
        cmds: Sequence[Sequence[str]],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
        check: bool = True,
        env: Mapping[str, str] | None = None,
        max_workers: int | None = None,
    ) -> list[subprocess.CompletedProcess[str]]: ...

    def run_all(
        self,
        cmds: Sequence[Sequence[str]] | Sequence[ShellCommand[T]],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
        check: bool = True,
        env: Mapping[str, str] | None = None,
        max_workers: int | None = None,
    ) -> list[subprocess.CompletedProcess[str]] | list[T]:
        """Run several commands concurrently and return their results in order.

        Python's analogue of ``Promise.all([...])`` for subprocess work:
        each command runs on its own thread (subprocess I/O releases the
        GIL while blocked), and results come back in the same order as
        ``cmds`` regardless of which finished first.

        Mirrors the two call shapes of :meth:`run`:

        * **argv batch** (``Sequence[Sequence[str]]``) — returns
          ``list[CompletedProcess[str]]``.
        * **:class:`ShellCommand` batch** (``Sequence[ShellCommand[T]]``)
          — each command's :meth:`~ShellCommand.parse` runs on the
          worker thread; returns ``list[T]``.

        The batch must be homogeneous: either all argv sequences or all
        :class:`ShellCommand` instances. Mixed batches are not supported
        by the static types (the return type would degrade to a union);
        if you need a mixed batch, call :meth:`run` from your own
        executor.

        All commands share the same ``cwd`` / ``timeout`` / ``env`` /
        ``check``; per-call differences are not supported by design — if
        you need them, schedule individual :meth:`run` calls onto a
        :class:`concurrent.futures.ThreadPoolExecutor` yourself.

        Parameters
        ----------
        cmds : Sequence[Sequence[str]] or Sequence[ShellCommand[T]]
            The commands to run. Either all argv lists/tuples, or all
            :class:`ShellCommand` instances.
        cwd : Path, optional
            Working directory applied to every command.
        timeout : int, optional
            Override the instance's :attr:`timeout` for every call.
        check : bool, optional
            If ``True`` (default), the first non-zero exit raises
            :class:`ShellError` and the call returns no results. Other
            subprocesses already running finish in the background; their
            outputs are discarded.
        env : Mapping[str, str], optional
            Override the instance's :attr:`env` for every call.
        max_workers : int, optional
            Cap on concurrent threads. ``None`` (default) lets
            :class:`ThreadPoolExecutor` pick its own default
            (``min(32, cpu_count + 4)``).

        Returns
        -------
        list[subprocess.CompletedProcess[str]] or list[T]
            Results in input order, one per command in ``cmds``. Element
            type follows the input: ``CompletedProcess`` for argv calls,
            the parser's return type for :class:`ShellCommand` calls.

        Raises
        ------
        ShellError
            If any call exits non-zero under ``check=True``. The first
            failure (in input order) is the one that propagates.
        subprocess.TimeoutExpired
            If any call exceeds ``timeout``.

        Examples
        --------
        >>> shell = Shell()  # doctest: +SKIP
        >>> results = shell.run_all(  # doctest: +SKIP
        ...     [
        ...         ["git", "rev-parse", "HEAD"],
        ...         ["git", "rev-parse", "--show-toplevel"],
        ...         ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        ...     ],
        ...     cwd=Path("."),
        ... )
        >>> [r.stdout.strip() for r in results]  # doctest: +SKIP
        ['<sha>', '<repo-root>', 'main']

        >>> shas: list[str] = shell.run_all(  # doctest: +SKIP
        ...     [GitRevParse("HEAD"), GitRevParse("HEAD~1")],
        ...     cwd=Path("."),
        ... )
        """
        if not cmds:
            return []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(
                    self.run,
                    cmd,
                    cwd=cwd,
                    timeout=timeout,
                    check=check,
                    env=env,
                )
                for cmd in cmds
            ]
            return [f.result() for f in futures]
