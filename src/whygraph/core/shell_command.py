"""Reusable argv + parser pairs runnable by :class:`whygraph.core.shell.Shell`.

Exposes :class:`ShellCommand`, a generic "argv + parser" pair that can be
constructed two ways:

1. **Inline.** Pass ``argv=`` and ``parse=`` to the constructor for
   one-shot commands — no subclass required.
2. **Subclass.** Override :meth:`ShellCommand.argv` and/or
   :meth:`ShellCommand.parse` for commands that take parameters or need
   stateful parsing.

A :class:`ShellCommand` instance is a *value* — not a coroutine, not a
future. It can be constructed in one module and executed elsewhere by
handing it to :meth:`Shell.run`, which dispatches on type: argv sequences
return raw :class:`~subprocess.CompletedProcess`, while
:class:`ShellCommand` instances return whatever the command's
:meth:`parse` produced.

Execution context (``cwd``, ``timeout``, ``env``, ``check``) is supplied
by the caller at run time, so the same command instance can be reused
across repos, working directories, and services.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from subprocess import CompletedProcess
from typing import Generic, TypeVar

T = TypeVar("T")


class ShellCommand(Generic[T]):
    """A typed shell invocation: argv plus a parser for the captured result.

    Supports two construction modes:

    1. **Inline** — pass ``argv`` and ``parse`` directly:

       >>> from pathlib import Path
       >>> cmd = ShellCommand(
       ...     argv=["git", "rev-parse", "--show-toplevel"],
       ...     parse=lambda r: Path(r.stdout.strip()),
       ... )
       >>> # top: Path = shell.run(cmd)

    2. **Subclass** — override :meth:`argv` and/or :meth:`parse` for
       commands with parameters or non-trivial parsing. Subclasses do
       NOT need to call ``super().__init__()`` — the class-level
       defaults (``_argv = _parse = None``) leave the constructor
       optional.

    The two modes can be mixed: a subclass may override only one of the
    methods and pass the other side via the constructor.

    Type Parameters
    ---------------
    T
        The type produced by :meth:`parse`. Flows through
        :meth:`Shell.run` so call sites get a typed result without
        ``cast``.

    Parameters
    ----------
    argv : Sequence[str], optional
        The argv to execute when this command runs. Stored as a tuple so
        it cannot be mutated after construction. If omitted, subclasses
        MUST override :meth:`argv`.
    parse : Callable[[CompletedProcess[str]], T], optional
        Callable invoked with the captured subprocess result. If
        omitted, subclasses MUST override :meth:`parse`.
    """

    # Class-level defaults so @dataclass subclasses that override the
    # methods but don't call super().__init__() still work.
    _argv: Sequence[str] | None = None
    _parse: Callable[[CompletedProcess[str]], T] | None = None

    def __init__(
        self,
        argv: Sequence[str] | None = None,
        parse: Callable[[CompletedProcess[str]], T] | None = None,
    ) -> None:
        if argv is not None:
            self._argv = tuple(argv)
        if parse is not None:
            self._parse = parse

    def argv(self) -> list[str]:
        """Return the argv to execute.

        Returns the constructor-supplied argv when present. Subclasses
        that omit ``argv=`` MUST override this method.

        Returns
        -------
        list[str]
            The argv list passed verbatim to :func:`subprocess.run`.

        Raises
        ------
        NotImplementedError
            If neither a constructor ``argv=`` nor a subclass override
            is in place.
        """
        if self._argv is None:
            raise NotImplementedError(
                f"{type(self).__name__}.argv() is not implemented and "
                "no argv= was passed to ShellCommand.__init__."
            )
        return list(self._argv)

    def parse(self, result: CompletedProcess[str]) -> T:
        """Convert the captured subprocess result into a typed value.

        Receives the full :class:`~subprocess.CompletedProcess` (not
        just ``stdout``) so implementations can inspect ``stderr`` or
        ``returncode`` when needed — for instance, commands that
        tolerate non-zero exits via ``check=False``.

        Not called if the subprocess raised
        :class:`whygraph.core.shell.ShellError` (i.e. under the default
        ``check=True``, a non-zero exit propagates before parsing).

        Parameters
        ----------
        result : subprocess.CompletedProcess[str]
            The completed subprocess with captured stdout/stderr.

        Returns
        -------
        T
            The parsed, typed result handed back to the
            :meth:`Shell.run` caller.

        Raises
        ------
        NotImplementedError
            If neither a constructor ``parse=`` nor a subclass override
            is in place.
        """
        if self._parse is None:
            raise NotImplementedError(
                f"{type(self).__name__}.parse() is not implemented and "
                "no parse= was passed to ShellCommand.__init__."
            )
        return self._parse(result)
