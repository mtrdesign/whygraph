"""Tests for the hidden ``whygraph release-check`` subcommand."""

from __future__ import annotations

import httpx
import pytest
from click.testing import CliRunner

from whygraph.cli import main
from whygraph.cli.commands import release_check

MANUAL = "<!-- whygraph:manual-upgrade -->"


def _rel(tag: str, body: str = "notes") -> dict:
    return {
        "tag_name": f"v{tag}",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-10-27T23:01:41Z",
        "body": body,
    }


RELEASES = [_rel("2.3.0"), _rel("2.2.1"), _rel("2.2.0")]


def _serve(monkeypatch, current, payload=None, status=200, error=False):
    def handler(request: httpx.Request) -> httpx.Response:
        if error:
            raise httpx.ConnectError("boom")
        return httpx.Response(status, json=RELEASES if payload is None else payload)

    monkeypatch.setattr(release_check, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(release_check, "running_version", lambda: current)


def _run(*args):
    return CliRunner().invoke(main, ["release-check", *args])


def _lines(result):
    return result.stdout.splitlines()


def test_update(monkeypatch) -> None:
    _serve(monkeypatch, (2, 2, 0))
    result = _run()
    assert result.exit_code == 0
    assert _lines(result) == [
        "status=update",
        "target=2.3.0",
        "manual=0",
        "verified=1",
    ]


def test_current(monkeypatch) -> None:
    _serve(monkeypatch, (2, 3, 0))
    result = _run()
    assert result.exit_code == 0
    assert _lines(result)[:2] == ["status=current", "target=2.3.0"]
    assert len(_lines(result)) == 4


def test_downgrade(monkeypatch) -> None:
    _serve(monkeypatch, (2, 3, 0))
    result = _run("--target", "2.2.1")
    assert result.exit_code == 0
    assert _lines(result)[:2] == ["status=downgrade", "target=2.2.1"]


def test_ahead(monkeypatch) -> None:
    _serve(monkeypatch, (2, 4, 0))
    result = _run()
    assert result.exit_code == 0
    assert _lines(result)[0] == "status=ahead"


def test_unknown_target(monkeypatch) -> None:
    _serve(monkeypatch, (2, 2, 0))
    result = _run("--target", "9.9.9")
    assert result.exit_code == 0
    assert _lines(result)[:2] == ["status=unknown", "target="]
    assert len(_lines(result)) == 4


def test_manual_marker_and_unverified(monkeypatch) -> None:
    payload = [_rel("2.3.0"), _rel("2.2.1", MANUAL), _rel("2.2.0")]
    _serve(monkeypatch, (2, 1, 0), payload)
    result = _run()
    assert result.exit_code == 0
    assert _lines(result) == [
        "status=update",
        "target=2.3.0",
        "manual=1",
        "verified=0",
    ]


@pytest.mark.parametrize("kwargs", [{"status": 500}, {"error": True}])
def test_unreachable_exits_1_with_one_stderr_line(monkeypatch, kwargs) -> None:
    _serve(monkeypatch, (2, 2, 0), **kwargs)
    result = _run()
    assert result.exit_code == 1
    assert result.stdout == ""
    err = result.stderr.strip().splitlines()
    assert len(err) == 1
    assert err[0].startswith("could not reach GitHub Releases (")


def test_malformed_target_exits_2(monkeypatch) -> None:
    _serve(monkeypatch, (2, 2, 0))
    result = _run("--target", "2.3")
    assert result.exit_code == 2
    assert result.stdout == ""
    assert len(result.stderr.strip().splitlines()) == 1


def test_hidden_from_help() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert "release-check" not in result.stdout
