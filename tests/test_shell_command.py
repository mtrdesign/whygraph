"""Tests for :class:`ShellCommand` and the :meth:`Shell.run` overload."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from whygraph.core.shell import Shell, ShellError
from whygraph.core.shell_command import ShellCommand


@dataclass
class _EchoCommand(ShellCommand[str]):
    """Echo a string and return it stripped of the trailing newline."""

    text: str

    def argv(self) -> list[str]:
        return ["echo", self.text]

    def parse(self, result: CompletedProcess[str]) -> str:
        return result.stdout.rstrip("\n")


@dataclass
class _ReturncodeCommand(ShellCommand[int]):
    """Run an argv and return the captured returncode."""

    cmd: list[str]
    parsed: bool = field(default=False)

    def argv(self) -> list[str]:
        return self.cmd

    def parse(self, result: CompletedProcess[str]) -> int:
        self.parsed = True
        return result.returncode


@dataclass
class _PwdCommand(ShellCommand[str]):
    """Return the stripped stdout of ``pwd``."""

    def argv(self) -> list[str]:
        return ["pwd"]

    def parse(self, result: CompletedProcess[str]) -> str:
        return result.stdout.rstrip("\n")


def test_argv_path_unchanged() -> None:
    """A plain argv call still returns a :class:`CompletedProcess`."""
    result = Shell().run(["echo", "hi"])
    assert isinstance(result, CompletedProcess)
    assert result.stdout == "hi\n"
    assert result.returncode == 0


def test_command_returns_parsed_value() -> None:
    """A :class:`ShellCommand` call returns the parsed value, not a CompletedProcess."""
    out = Shell().run(_EchoCommand("hi"))
    assert out == "hi"
    assert isinstance(out, str)


def test_parse_receives_full_completed_process() -> None:
    """:meth:`ShellCommand.parse` gets the whole CompletedProcess, not just stdout."""
    rc = Shell().run(_ReturncodeCommand(cmd=["true"]))
    assert rc == 0


def test_nonzero_raises_before_parse() -> None:
    """Under default ``check=True``, a non-zero exit raises before parsing."""
    cmd = _ReturncodeCommand(cmd=["false"])
    with pytest.raises(ShellError):
        Shell().run(cmd)
    assert cmd.parsed is False


def test_check_false_lets_parse_see_failure() -> None:
    """With ``check=False``, :meth:`parse` runs and sees the non-zero result."""
    cmd = _ReturncodeCommand(cmd=["false"])
    rc = Shell().run(cmd, check=False)
    assert cmd.parsed is True
    assert rc != 0


def test_cwd_is_honored_for_command_path(tmp_path: Path) -> None:
    """``cwd=`` is forwarded into the subprocess for ShellCommand calls."""
    out = Shell().run(_PwdCommand(), cwd=tmp_path)
    # macOS resolves /var → /private/var via realpath in some contexts;
    # compare resolved paths so the test is stable on darwin.
    assert Path(out).resolve() == tmp_path.resolve()


def test_run_all_argv_path_unchanged() -> None:
    """An argv batch through ``run_all`` still returns CompletedProcess values."""
    results = Shell().run_all([["echo", "a"], ["echo", "b"]])
    assert len(results) == 2
    assert all(isinstance(r, CompletedProcess) for r in results)
    assert [r.stdout for r in results] == ["a\n", "b\n"]


def test_run_all_command_path_returns_parsed_list_in_input_order() -> None:
    """A ShellCommand batch returns parser outputs in the same order as input."""
    results = Shell().run_all(
        [_EchoCommand("a"), _EchoCommand("b"), _EchoCommand("c")]
    )
    assert results == ["a", "b", "c"]
    assert all(isinstance(r, str) for r in results)


def test_run_all_empty_returns_empty_list() -> None:
    """An empty batch short-circuits without spawning a pool."""
    assert Shell().run_all([]) == []


def test_run_all_command_path_raises_on_first_failure() -> None:
    """A failing command inside a batch propagates ShellError under check=True."""
    batch = [
        _ReturncodeCommand(cmd=["true"]),
        _ReturncodeCommand(cmd=["false"]),
        _ReturncodeCommand(cmd=["true"]),
    ]
    with pytest.raises(ShellError):
        Shell().run_all(batch)


# --- Inline construction (constructor-supplied argv + parse) -------------


def test_inline_construction_returns_parsed_value() -> None:
    """A ShellCommand built with argv= and parse= runs and returns the parsed value."""
    cmd = ShellCommand(
        argv=["echo", "hi"],
        parse=lambda r: r.stdout.rstrip("\n"),
    )
    assert Shell().run(cmd) == "hi"


def test_bare_shellcommand_raises_not_implemented_on_argv() -> None:
    """A ShellCommand with no argv= and no override raises on argv()."""
    with pytest.raises(NotImplementedError, match="argv"):
        ShellCommand().argv()


def test_bare_shellcommand_raises_not_implemented_on_parse() -> None:
    """A ShellCommand with no parse= and no override raises on parse()."""
    result = CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with pytest.raises(NotImplementedError, match="parse"):
        ShellCommand().parse(result)


def test_inline_argv_is_defensively_copied() -> None:
    """Mutating the argv list after construction must not affect later runs."""
    original = ["echo", "hi"]
    cmd = ShellCommand(argv=original, parse=lambda r: r.stdout.rstrip("\n"))
    original.append("bye")
    assert Shell().run(cmd) == "hi"
