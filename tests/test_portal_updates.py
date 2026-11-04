"""The portal's release check (plan 43 sections 4.2, 4.4; section 9 cases 5-7).

The switch (:func:`whygraph.portal.updates.check_switch`), the in-memory
:class:`~whygraph.portal.updates.UpdateBook` and its wire view, the fetch
into the book, and the lifespan's ``_check_for_updates`` loop - one fetch
per due check, never fatal, and abandoned on cancel so it cannot delay a
shutdown. No test here reaches the network: every fetch goes through an
``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import anyio
import httpx
import pytest

from whygraph.core.releases import NOTES_URL, Release
from whygraph.portal import app as portal_app
from whygraph.portal import db as portal_db
from whygraph.portal import updates
from whygraph.portal.models import Setting
from whygraph.portal.updates import UpdateBook, check_switch

NOW = datetime(2026, 11, 3, 9, 0, 0, tzinfo=UTC)


def _release(version: str, *, age_hours: float = 48, manual: bool = False) -> Release:
    major, minor, patch = (int(part) for part in version.split("."))
    return Release(
        version=(major, minor, patch),
        published_at=NOW - timedelta(hours=age_hours),
        manual=manual,
    )


def _feed(*versions: str) -> list[dict]:
    """A release list as GitHub returns it (only the fields that are read)."""
    return [
        {
            "tag_name": f"v{version}",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-10-01T00:00:00Z",
            "body": "notes",
        }
        for version in versions
    ]


@pytest.fixture(autouse=True)
def _reset_warned() -> None:
    updates._warned.clear()


# ---------------------------------------------------------------------------
# Case 5: the switch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "mode", "setting_on", "expected"),
    [
        ({}, "production", True, (True, "default")),
        ({}, "local", True, (True, "setting")),
        ({}, "local", False, (False, "setting")),
        ({"DO_NOT_TRACK": "1"}, "local", True, (False, "do_not_track")),
        ({"DO_NOT_TRACK": " TRUE "}, "production", True, (False, "do_not_track")),
        # DO_NOT_TRACK wins over an explicit "on".
        (
            {"DO_NOT_TRACK": "yes", "WHYGRAPH_UPDATE_CHECK": "1"},
            "local",
            True,
            (False, "do_not_track"),
        ),
        # A falsy DO_NOT_TRACK does not count.
        ({"DO_NOT_TRACK": "0"}, "production", True, (True, "default")),
        ({"DO_NOT_TRACK": "off"}, "local", False, (False, "setting")),
        # The environment wins over the setting, both ways.
        ({"WHYGRAPH_UPDATE_CHECK": "0"}, "local", True, (False, "env")),
        ({"WHYGRAPH_UPDATE_CHECK": "On"}, "local", False, (True, "env")),
        ({"WHYGRAPH_UPDATE_CHECK": "no"}, "production", True, (False, "env")),
        ({"WHYGRAPH_UPDATE_CHECK": "true"}, "production", True, (True, "env")),
        # Empty and unknown values are ignored.
        ({"WHYGRAPH_UPDATE_CHECK": ""}, "local", False, (False, "setting")),
        ({"WHYGRAPH_UPDATE_CHECK": "maybe"}, "local", False, (False, "setting")),
        ({"DO_NOT_TRACK": "please"}, "production", True, (True, "default")),
    ],
)
def test_check_switch_table(
    env: dict[str, str], mode: str, setting_on: bool, expected: tuple[bool, str]
) -> None:
    assert check_switch(mode, setting_on, env) == expected


def test_check_switch_reads_the_environment_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The autouse guard turns the check off for every test.
    assert check_switch("production", True) == (False, "env")
    monkeypatch.delenv("WHYGRAPH_UPDATE_CHECK")
    assert check_switch("production", True) == (True, "default")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    assert check_switch("production", True) == (False, "do_not_track")


def test_an_unknown_value_is_logged_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The log-once memory is process-wide; start from a clean one so an
    # earlier test in the same worker cannot have used up the warning.
    monkeypatch.setattr(updates, "_warned", set())
    env = {"WHYGRAPH_UPDATE_CHECK": "maybe"}
    with caplog.at_level(logging.WARNING, logger=updates.__name__):
        for _ in range(3):
            assert check_switch("local", True, env) == (True, "setting")
    warnings = [r for r in caplog.records if "WHYGRAPH_UPDATE_CHECK" in r.getMessage()]
    assert len(warnings) == 1


def test_read_switch_in_production_needs_no_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WHYGRAPH_UPDATE_CHECK")
    state = SimpleNamespace(mode="production")
    assert updates.read_switch(state) == (True, "default")


def test_read_switch_in_local_mode_reads_the_setting(
    portal_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WHYGRAPH_UPDATE_CHECK")
    state = SimpleNamespace(mode="local")
    # No settings row yet: on.
    assert updates.read_switch(state) == (True, "setting")
    with portal_db.get_session() as session:
        session.add(Setting(id=1, mode="local", update_check=False))
    assert updates.read_switch(state) == (False, "setting")
    monkeypatch.setenv("WHYGRAPH_UPDATE_CHECK", "1")
    assert updates.read_switch(state) == (True, "env")


# ---------------------------------------------------------------------------
# Case 6: the book
# ---------------------------------------------------------------------------


def test_due_after_record_and_after_fail() -> None:
    book = UpdateBook()
    book.set_switch((True, "default"))
    assert book.due(time.monotonic())
    gen = book.begin()
    book.record(gen, [_release("2.3.0")], 1000.0, NOW)
    assert not book.due(1000.0 + updates.CHECK_EVERY_SEC - 1)
    assert book.due(1000.0 + updates.CHECK_EVERY_SEC)
    book.fail(book.begin(), "network", 5000.0, NOW)
    assert not book.due(5000.0 + updates.RETRY_EVERY_SEC - 1)
    assert book.due(5000.0 + updates.RETRY_EVERY_SEC)


def test_fail_keeps_the_releases() -> None:
    book = UpdateBook()
    book.set_switch((True, "default"))
    book.record(book.begin(), [_release("2.3.0")], 1.0, NOW)
    later = NOW + timedelta(hours=1)
    book.fail(book.begin(), "rate_limited", 2.0, later)
    assert book.releases == (_release("2.3.0"),)
    assert book.error == "rate_limited"
    assert book.checked_at == later
    view = book.view((2, 2, 1), NOW)
    assert view is not None
    assert view["latest"] == "2.3.0"
    assert view["available"] is True
    assert view["error"] == "rate_limited"
    # A success clears the error.
    book.record(book.begin(), [_release("2.3.0")], 3.0, NOW)
    assert book.error is None


def test_flipping_the_switch_clears_the_book_and_makes_it_due() -> None:
    book = UpdateBook()
    book.set_switch((True, "default"))
    first = book.begin()
    book.record(first, [_release("2.3.0")], 1000.0, NOW)
    assert not book.due(1000.0)

    # Same enabled flag, another source: nothing resets.
    book.set_switch((True, "setting"))
    assert book.begin() == first
    assert book.releases
    assert not book.due(1000.0)

    book.set_switch((False, "setting"))
    off = book.begin()
    assert off > first
    assert book.releases == ()
    assert book.checked_at is None
    assert book.error is None

    book.set_switch((True, "setting"))
    assert book.begin() > off
    assert book.due(1000.0)


def test_a_stale_generation_lands_nowhere() -> None:
    book = UpdateBook()
    book.set_switch((True, "default"))
    gen = book.begin()  # a fetch starts ...
    book.set_switch((False, "setting"))  # ... the check is turned off ...
    book.record(gen, [_release("2.3.0")], 1.0, NOW)  # ... and the fetch ends.
    assert book.releases == ()
    assert book.checked_at is None
    book.fail(gen, "network", 1.0, NOW)
    assert book.error is None
    # Turned back on: the stale result still lands nowhere, and a check is due.
    book.set_switch((True, "setting"))
    book.record(gen, [_release("2.3.0")], 1.0, NOW)
    assert book.releases == ()
    assert book.due(1.0)


def test_a_switch_stored_by_a_put_beats_the_loops_stale_read() -> None:
    book = UpdateBook()
    book.set_switch((True, "default"))
    epoch = book.epoch()  # the loop starts reading the setting (still on) ...
    assert book.set_switch((False, "setting"))  # ... a PUT turns it off ...
    # ... and the loop's stale "on" is not stored.
    assert not book.set_switch((True, "setting"), if_epoch=epoch)
    assert book.switch == (False, "setting")
    # A read that started after the PUT is stored.
    assert book.set_switch((False, "setting"), if_epoch=book.epoch())


def test_view_is_none_before_the_switch_is_known() -> None:
    assert UpdateBook().view((2, 2, 1), NOW) is None


def test_view_when_disabled() -> None:
    book = UpdateBook()
    book.set_switch((False, "do_not_track"))
    assert book.view((2, 2, 1), NOW) == {
        "enabled": False,
        "source": "do_not_track",
        "current": "2.2.1",
        "latest": None,
        "available": False,
        "manual_steps": False,
        "notes_url": None,
        "upgrading_url": updates.UPGRADING_URL,
        "checked_at": None,
        "error": None,
    }
    assert book.view(None, NOW)["current"] is None  # type: ignore[index]


def test_view_with_an_update() -> None:
    book = UpdateBook()
    book.set_switch((True, "setting"))
    book.record(
        book.begin(),
        [_release("2.3.0", age_hours=2), _release("2.2.1"), _release("2.2.0")],
        1.0,
        datetime(2026, 11, 3, 8, 30, 15, 123456, tzinfo=UTC),
    )
    assert book.view((2, 2, 1), NOW) == {
        "enabled": True,
        "source": "setting",
        "current": "2.2.1",
        "latest": "2.3.0",
        "available": True,
        "manual_steps": False,
        "notes_url": NOTES_URL.format(version="2.3.0"),
        "upgrading_url": updates.UPGRADING_URL,
        "checked_at": "2026-11-03T08:30:15Z",
        "error": None,
    }


def test_view_applies_the_grace_on_read() -> None:
    book = UpdateBook()
    book.set_switch((True, "default"))
    book.record(
        book.begin(),
        [_release("2.3.0", age_hours=0.5), _release("2.2.1")],
        1.0,
        NOW,
    )
    early = book.view((2, 2, 1), NOW)
    assert early is not None
    assert early["available"] is False
    assert early["latest"] == "2.2.1"
    late = book.view((2, 2, 1), NOW + timedelta(hours=2))  # no new fetch
    assert late is not None
    assert late["available"] is True
    assert late["latest"] == "2.3.0"


def test_view_manual_steps() -> None:
    book = UpdateBook()
    book.set_switch((True, "default"))
    # A marker on an in-between release.
    book.record(
        book.begin(),
        [_release("2.4.0"), _release("2.3.0", manual=True), _release("2.2.1")],
        1.0,
        NOW,
    )
    view = book.view((2, 2, 1), NOW)
    assert view is not None
    assert (view["available"], view["manual_steps"]) == (True, True)
    # Unverified: the list does not reach back to the running version.
    book.record(book.begin(), [_release("2.4.0"), _release("2.3.0")], 2.0, NOW)
    view = book.view((2, 2, 1), NOW)
    assert view is not None
    assert (view["available"], view["manual_steps"]) == (True, True)
    # Up to date: no notice.
    book.record(book.begin(), [_release("2.2.1"), _release("2.2.0")], 3.0, NOW)
    view = book.view((2, 2, 1), NOW)
    assert view is not None
    assert view["latest"] == "2.2.1"
    assert (view["available"], view["manual_steps"]) == (False, False)


def test_view_without_releases() -> None:
    book = UpdateBook()
    book.set_switch((True, "default"))
    view = book.view((2, 2, 1), NOW)
    assert view is not None
    assert view["enabled"] is True
    assert (view["latest"], view["available"], view["manual_steps"]) == (
        None,
        False,
        False,
    )


def test_update_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updates, "running_version", lambda: (2, 2, 1))
    state = SimpleNamespace(updates=UpdateBook())
    assert updates.update_block(state) is None
    state.updates.set_switch((False, "env"))
    block = updates.update_block(state)
    assert block is not None
    assert (block["enabled"], block["source"], block["current"]) == (
        False,
        "env",
        "2.2.1",
    )


# ---------------------------------------------------------------------------
# The fetch into the book
# ---------------------------------------------------------------------------


def test_fetch_into_book_records_the_releases() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_feed("2.2.0", "2.3.0"))

    book = UpdateBook()
    book.set_switch((True, "default"))
    updates.fetch_into_book(book, book.begin(), httpx.MockTransport(handle))
    assert [rel.version for rel in book.releases] == [(2, 3, 0), (2, 2, 0)]
    assert book.error is None
    assert book.checked_at is not None
    assert not book.due(time.monotonic())
    assert len(seen) == 1
    assert seen[0].headers["user-agent"] == updates.USER_AGENT


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(403), "rate_limited"),
        (httpx.Response(429), "rate_limited"),
        (httpx.Response(500), "bad_response"),
        (httpx.Response(200, content=b"not json"), "bad_response"),
        (httpx.Response(200, content=json.dumps({"a": 1}).encode()), "bad_response"),
    ],
)
def test_fetch_into_book_fails_and_keeps_the_releases(
    response: httpx.Response, kind: str
) -> None:
    book = UpdateBook()
    book.set_switch((True, "default"))
    book.record(book.begin(), [_release("2.3.0")], 1.0, NOW)
    updates.fetch_into_book(
        book, book.begin(), httpx.MockTransport(lambda request: response)
    )
    assert book.error == kind
    assert book.releases == (_release("2.3.0"),)
    assert not book.due(time.monotonic())


def test_fetch_into_book_never_raises() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("boom")

    book = UpdateBook()
    book.set_switch((True, "default"))
    updates.fetch_into_book(book, book.begin(), httpx.MockTransport(handle))
    assert book.error == "network"


# ---------------------------------------------------------------------------
# Case 7: the loop
# ---------------------------------------------------------------------------


def _loop_state(handler) -> SimpleNamespace:  # noqa: ANN001
    return SimpleNamespace(
        mode="production",
        updates=UpdateBook(),
        update_transport=httpx.MockTransport(handler),
    )


def test_the_loop_fetches_once_when_due(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WHYGRAPH_UPDATE_CHECK")
    monkeypatch.setattr(updates, "TICK_SEC", 0.01)
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_feed("2.3.0"))

    state = _loop_state(handle)

    async def run() -> None:
        with anyio.move_on_after(0.5):
            await portal_app._check_for_updates(state)

    anyio.run(run)
    assert len(seen) == 1  # many ticks, one due check
    assert state.updates.switch == (True, "default")
    assert [rel.version for rel in state.updates.releases] == [(2, 3, 0)]


def test_the_loop_does_not_fetch_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updates, "TICK_SEC", 0.01)
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_feed("2.3.0"))

    state = _loop_state(handle)

    async def run() -> None:
        with anyio.move_on_after(0.2):
            await portal_app._check_for_updates(state)

    anyio.run(run)
    assert seen == []
    assert state.updates.switch == (False, "env")
    view = state.updates.view((2, 2, 1), NOW)
    assert view is not None
    assert view["enabled"] is False


def test_the_loop_survives_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WHYGRAPH_UPDATE_CHECK")
    monkeypatch.setattr(updates, "TICK_SEC", 0.01)
    real = updates.read_switch
    calls: list[int] = []

    def flaky(state: object) -> tuple[bool, str]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database down")
        return real(state)  # type: ignore[arg-type]

    monkeypatch.setattr(updates, "read_switch", flaky)
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(429)

    state = _loop_state(handle)

    async def run() -> None:
        with anyio.move_on_after(0.5):
            await portal_app._check_for_updates(state)

    anyio.run(run)
    assert len(calls) > 2
    assert len(seen) == 1  # a failed check is retried hours later, not every tick
    assert state.updates.error == "rate_limited"


def test_the_loop_abandons_a_hung_fetch_on_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WHYGRAPH_UPDATE_CHECK")
    started = threading.Event()
    release = threading.Event()

    def handle(request: httpx.Request) -> httpx.Response:
        started.set()
        release.wait(10)
        return httpx.Response(200, json=_feed("2.3.0"))

    state = _loop_state(handle)
    elapsed: list[float] = []

    async def run() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(portal_app._check_for_updates, state)
            with anyio.fail_after(5):
                while not started.is_set():
                    await anyio.sleep(0.01)
            begun = time.monotonic()
            tg.cancel_scope.cancel()
        elapsed.append(time.monotonic() - begun)
        # The abandoned thread finishes later and only reaches the book.
        release.set()
        with anyio.fail_after(5):
            while state.updates.checked_at is None:
                await anyio.sleep(0.01)

    try:
        anyio.run(run)
    finally:
        release.set()
    assert elapsed[0] < 1.0
    assert [rel.version for rel in state.updates.releases] == [(2, 3, 0)]
