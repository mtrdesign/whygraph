"""Tests for ``[scan].provider`` gating in the scan command.

Pins :func:`whygraph.cli.commands.scan._select_github_client` — the small
resolver that maps the configured provider to a GitHub client (or
``None``). ``"off"`` must short-circuit *before* touching
:meth:`GitHubClient.for_repository`; ``"github"`` / ``"auto"`` must
delegate to it.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from whygraph.cli.commands.scan import (
    _apply_github_token,
    _github_skip_reason,
    _select_github_client,
)


def _cfg(provider: str, token: str | None) -> SimpleNamespace:
    """Minimal stand-in exposing the two attrs ``_apply_github_token`` reads."""
    return SimpleNamespace(scan_provider=provider, scan_token=token)


def test_provider_off_returns_none_without_calling_for_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def _boom(cls, repo):  # pragma: no cover - must never run
        calls.append(repo)
        raise AssertionError("for_repository must not be called when provider=off")

    monkeypatch.setattr(
        "whygraph.services.github.GitHubClient.for_repository",
        classmethod(_boom),
    )

    assert _select_github_client("off", repository=object()) is None
    assert calls == []


@pytest.mark.parametrize("provider", ["github", "auto"])
def test_provider_delegates_to_for_repository(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    sentinel = object()
    seen: list[object] = []

    def _resolve(cls, repo):
        seen.append(repo)
        return sentinel

    monkeypatch.setattr(
        "whygraph.services.github.GitHubClient.for_repository",
        classmethod(_resolve),
    )

    repo = object()
    assert _select_github_client(provider, repository=repo) is sentinel
    assert seen == [repo]


def test_token_off_provider_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    _apply_github_token(_cfg("off", "ghp_should_be_ignored"))

    assert "GH_TOKEN" not in os.environ


def test_token_from_config_exported_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    _apply_github_token(_cfg("github", "ghp_from_toml"))

    assert os.environ["GH_TOKEN"] == "ghp_from_toml"


def test_existing_gh_token_left_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_ambient")

    _apply_github_token(_cfg("github", "ghp_from_toml"))

    assert os.environ["GH_TOKEN"] == "ghp_ambient"


def test_github_token_env_promoted_to_gh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_github_env")

    _apply_github_token(_cfg("auto", None))

    assert os.environ["GH_TOKEN"] == "ghp_github_env"


def test_skip_reason_no_remote_takes_precedence() -> None:
    # Even with provider="github", --no-remote is the reason shown.
    assert _github_skip_reason(_cfg("github", None), False) == "skipped — --no-remote"


def test_skip_reason_falls_back_to_provider_when_remote_enabled() -> None:
    assert "provider = off" in _github_skip_reason(_cfg("off", None), True)
