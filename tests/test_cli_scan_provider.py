"""Tests for ``[scan].provider`` gating in the scan command.

Pins :func:`whygraph.cli.commands.scan._select_github_client` — the small
resolver that maps the configured provider to a GitHub client (or
``None``). ``"off"`` must short-circuit *before* touching
:meth:`GitHubClient.for_repository`; ``"github"`` / ``"auto"`` must
delegate to it.
"""

from __future__ import annotations

import pytest

from whygraph.cli.commands.scan import _select_github_client


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
