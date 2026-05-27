"""Preflight diagnostics for the WhyGraph CLI.

Runs as the first step of ``whygraph init``: probes the host for the tools
the rest of the workflow expects (``git``, ``docker``, ``gh``, an LLM
credential), prints a one-line status per check, and raises
:class:`PreflightError` if a required tool is missing.

Hard-required tools are collected and reported together so a fresh user
sees the full punch list once. Soft checks print as warnings and don't
affect exit code.

Designed to be importable from other commands later (``scan``) without
restructuring — :func:`run_preflight` is the only public surface.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .console import console


class PreflightError(RuntimeError):
    """Raised when one or more hard-required tools are missing from PATH.

    The message names every missing tool; the inline preflight output
    printed before the raise carries the install hints.
    """


@dataclass(frozen=True)
class _CheckResult:
    """Outcome of one tool probe.

    Attributes
    ----------
    name : str
        Label shown in the preflight block (e.g. ``"git"``, ``"LLM provider"``).
    status : str
        One of ``"ok"``, ``"missing"``, ``"skipped"``.
    hint : str, optional
        Human-readable install hint shown for ``"missing"`` results.
    soft : bool
        When ``True``, a ``"missing"`` result is a warning only; the
        caller does not raise.
    """

    name: str
    status: str
    hint: str | None = None
    soft: bool = False


_GLYPH = {"ok": "✓", "missing": "✗", "skipped": "—"}


def run_preflight(project_root: Path, *, with_codegraph: bool) -> None:
    """Echo a preflight checks block; raise if a hard requirement is missing.

    Parameters
    ----------
    project_root : Path
        Repository root — used to detect whether the project has a
        ``github.com`` remote (which makes the ``gh`` probe relevant).
    with_codegraph : bool
        If ``True``, ``docker`` is treated as a hard requirement (the
        CodeGraph bootstrap step needs it). If ``False`` — i.e. the user
        passed ``--no-codegraph`` — the docker probe is silently skipped.

    Raises
    ------
    PreflightError
        If ``git`` is missing, or if ``docker`` is missing while
        ``with_codegraph`` is ``True``. All missing hard requirements
        are reported in a single error.
    """
    checks = [
        _check_git(),
        _check_docker(required=with_codegraph),
        _check_gh(project_root),
        _check_llm(),
    ]

    console.print("Preflight checks:")
    for c in checks:
        glyph = _GLYPH[c.status]
        tag = "  (optional)" if c.status == "missing" and c.soft else ""
        console.print(f"  {c.name:<14}{glyph}{tag}")
        if c.status == "missing" and c.hint:
            console.print(f"                  install: {c.hint}")

    hard_missing = [
        c.name for c in checks if c.status == "missing" and not c.soft
    ]
    if hard_missing:
        names = ", ".join(hard_missing)
        raise PreflightError(
            f"required tool(s) missing: {names} —"
            " see preflight output above for install hints"
        )


def _check_git() -> _CheckResult:
    if shutil.which("git") is None:
        return _CheckResult(
            name="git",
            status="missing",
            hint="brew install git (macOS) / apt install git (Debian/Ubuntu)",
        )
    return _CheckResult(name="git", status="ok")


def _check_docker(*, required: bool) -> _CheckResult:
    if not required:
        return _CheckResult(name="docker", status="skipped", soft=True)
    if shutil.which("docker") is None:
        return _CheckResult(
            name="docker",
            status="missing",
            hint=(
                "install Docker Desktop"
                " (https://www.docker.com/products/docker-desktop/),"
                " or pass --no-codegraph to skip the CodeGraph bootstrap"
            ),
        )
    return _CheckResult(name="docker", status="ok")


def _check_gh(project_root: Path) -> _CheckResult:
    if not _has_github_remote(project_root):
        return _CheckResult(name="gh", status="skipped", soft=True)

    if shutil.which("gh") is None:
        return _CheckResult(
            name="gh",
            status="missing",
            hint="brew install gh && gh auth login",
            soft=True,
        )

    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return _CheckResult(
            name="gh",
            status="missing",
            hint="brew install gh && gh auth login",
            soft=True,
        )

    if result.returncode != 0:
        return _CheckResult(
            name="gh",
            status="missing",
            hint="gh auth login",
            soft=True,
        )
    return _CheckResult(name="gh", status="ok")


def _check_llm() -> _CheckResult:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _CheckResult(name="LLM provider", status="ok")
    if shutil.which("claude") is not None:
        return _CheckResult(name="LLM provider", status="ok")
    return _CheckResult(
        name="LLM provider",
        status="missing",
        hint=(
            "set ANTHROPIC_API_KEY for the Anthropic SDK path,"
            " or install the `claude` CLI for subscription billing"
        ),
        soft=True,
    )


def _has_github_remote(project_root: Path) -> bool:
    """Cheap parse of ``.git/config`` — return ``True`` if any URL contains ``github.com``.

    Avoids shelling out to ``git remote -v`` so the probe is fast and
    works on a checkout where ``git`` isn't even installed yet (the
    ``git`` check upstream of this one will surface that case).
    """
    cfg = project_root / ".git" / "config"
    if not cfg.exists():
        return False
    try:
        return "github.com" in cfg.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


__all__ = ["PreflightError", "run_preflight"]
