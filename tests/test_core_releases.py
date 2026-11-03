"""Release resolution (plan 43 sections 4.1 and 9, cases 1-4).

Unit tests for :mod:`whygraph.core.releases`: strict version parsing, the
hardened fetch of GitHub's release list against ``httpx.MockTransport``
(never the network), the request's headers, and :func:`resolve`'s grace,
comparison, manual-marker and ``verified`` rules.
"""

from __future__ import annotations

import gzip
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import httpx
import pytest

from whygraph.core import releases as rel_mod
from whygraph.core.releases import (
    FEED_URL,
    MANUAL_MARKER,
    MAX_RESPONSE_BYTES,
    NOTES_URL,
    PAGE_SIZE,
    Release,
    ReleaseCheckError,
    Resolution,
    build_client,
    fetch_releases,
    format_version,
    parse_version,
    resolve,
    running_version,
)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def item(tag: str = "v2.2.1", **over: Any) -> dict:
    """A release item shaped like GitHub's (a subset of the measured fields)."""
    base = {
        "url": "https://api.github.com/repos/mtrdesign/whygraph/releases/1",
        "html_url": f"https://github.com/mtrdesign/whygraph/releases/tag/{tag}",
        "id": 1,
        "tag_name": tag,
        "target_commitish": "main",
        "name": tag,
        "draft": False,
        "immutable": False,
        "prerelease": False,
        "created_at": "2026-10-27T22:58:12Z",
        "updated_at": "2026-10-27T23:01:41Z",
        "published_at": "2026-10-27T23:01:41Z",
        "assets": [],
        "tarball_url": "https://api.github.com/repos/mtrdesign/whygraph/tarball/x",
        "zipball_url": "https://api.github.com/repos/mtrdesign/whygraph/zipball/x",
        "body": "WhyGraph release.\n\nNothing to do when upgrading.",
    }
    base.update(over)
    return base


def minified(data: Any) -> bytes:
    """JSON as GitHub sends it to a non-curl client: one line, no spaces."""
    return json.dumps(data, separators=(",", ":")).encode()


def client_for(handler) -> httpx.Client:
    """A ``build_client`` client over a mock transport."""
    return build_client("whygraph-cli", transport=httpx.MockTransport(handler))


def fetch(body: bytes | None = None, *, status: int = 200, **kwargs) -> list[Release]:
    """Fetch through a transport answering ``status`` with ``body``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body or b"", **kwargs)

    with client_for(handler) as client:
        return fetch_releases(client)


def fetch_kind(*args, **kwargs) -> str:
    """The ``ReleaseCheckError`` kind a fetch raises."""
    with pytest.raises(ReleaseCheckError) as info:
        fetch(*args, **kwargs)
    assert info.value.args == (info.value.kind,)
    return info.value.kind


def at(minutes_ago: float) -> datetime:
    """``NOW`` minus some minutes."""
    return NOW - timedelta(minutes=minutes_ago)


def r(version: str, *, minutes_ago: float = 24 * 60, manual: bool = False) -> Release:
    """A ``Release`` for :func:`resolve`."""
    parsed = parse_version(version)
    assert parsed is not None
    return Release(version=parsed, published_at=at(minutes_ago), manual=manual)


# --- 1. parse_version / format_version ---------------------------------------


def test_parse_version_accepts_plain_semver():
    assert parse_version("2.3.0") == (2, 3, 0)
    assert parse_version("0.0.0") == (0, 0, 0)
    assert parse_version("10.0.0") == (10, 0, 0)
    assert parse_version("9999.9999.9999") == (9999, 9999, 9999)


@pytest.mark.parametrize(
    "text",
    [
        "02.3.0",
        "2.03.0",
        "2.3.00",
        "2.3",
        "2.3.0.1",
        "2.3.0-rc1",
        "2.3.0\n",
        " 2.3.0",
        "2.3.0 ",
        "v2.3.0",
        "٢.3.0",  # ARABIC-INDIC DIGIT TWO
        "2.３.0",  # FULLWIDTH DIGIT THREE
        "10000.0.0",
        "",
        "a.b.c",
    ],
)
def test_parse_version_rejects(text):
    assert parse_version(text) is None


def test_format_version_round_trips_and_builds_notes_url():
    assert format_version((2, 3, 0)) == "2.3.0"
    assert format_version(parse_version("10.0.12")) == "10.0.12"
    assert (
        NOTES_URL.format(version=format_version((2, 2, 1)))
        == "https://mtrdesign.github.io/whygraph/releases/2.2.1/"
    )


def test_running_version(monkeypatch):
    monkeypatch.setattr(rel_mod, "_pkg_version", lambda name: "2.2.1")
    assert running_version() == (2, 2, 1)
    monkeypatch.setattr(rel_mod, "_pkg_version", lambda name: "2.2.1.dev3")
    assert running_version() is None

    def missing(name):
        raise rel_mod.PackageNotFoundError(name)

    monkeypatch.setattr(rel_mod, "_pkg_version", missing)
    assert running_version() is None


def test_error_kind_attribute():
    err = ReleaseCheckError("network")
    assert err.kind == "network"
    assert str(err) == "network"


# --- 2. fetch_releases --------------------------------------------------------


def test_fetch_valid_list_newest_first():
    body = minified(
        [
            item("v2.2.0", published_at="2026-10-24T10:00:00Z"),
            item("v2.2.1", body=f"Notes.\n\n{MANUAL_MARKER}\n"),
            item("v10.0.0", published_at="2026-10-30T08:15:00+02:00"),
            item("v2.0.0", body=None),
        ]
    )
    releases = fetch(body)
    assert [x.version for x in releases] == [
        (10, 0, 0),
        (2, 2, 1),
        (2, 2, 0),
        (2, 0, 0),
    ]
    by_version = {x.version: x for x in releases}
    assert by_version[(2, 2, 1)].manual is True
    assert by_version[(2, 2, 0)].manual is False
    assert by_version[(2, 0, 0)].manual is False
    assert by_version[(2, 2, 1)].published_at == datetime(
        2026, 10, 27, 23, 1, 41, tzinfo=timezone.utc
    )
    assert by_version[(10, 0, 0)].published_at.utcoffset() == timedelta(hours=2)


def test_fetch_keeps_only_the_three_fields():
    (only,) = fetch(minified([item("v2.2.1")]))
    assert set(vars(only)) == {"version", "published_at", "manual"}


@pytest.mark.parametrize(
    "bad",
    [
        item("v9.0.0", draft=True),
        item("v9.0.0", prerelease=True),
        item("v9.0.0", draft="false"),
        item("v9.0.0", prerelease="false"),
        item("v9.0.0", prerelease=0),
        {k: v for k, v in item("v9.0.0").items() if k != "draft"},
        item("9.0.0"),
        item("V9.0.0"),
        item("v9.0"),
        item("v9.0.0-rc1"),
        item("v09.0.0"),
        item(tag_name=None),
        item(tag_name=["v9.0.0"]),
        item("v9.0.0", published_at="2026-10-27T23:01:41"),
        item("v9.0.0", published_at="2026-10-27"),
        item("v9.0.0", published_at="yesterday"),
        item("v9.0.0", published_at=None),
        item("v9.0.0", published_at=1730000000),
        "v9.0.0",
        ["v9.0.0"],
        None,
        42,
    ],
)
def test_fetch_skips_a_bad_item_and_keeps_the_rest(bad):
    releases = fetch(minified([item("v2.2.1"), bad, item("v2.2.0")]))
    assert [x.version for x in releases] == [(2, 2, 1), (2, 2, 0)]


def test_fetch_manual_needs_a_string_body():
    (only,) = fetch(minified([item("v2.2.1", body=[MANUAL_MARKER])]))
    assert only.manual is False


def test_fetch_empty_list():
    assert fetch(b"[]") == []


@pytest.mark.parametrize(
    "body",
    [
        minified({"message": "Not Found"}),
        minified("v2.2.1"),
        minified(None),
        b"",
        b"<html>nope</html>",
        b"[" * 100_000,  # RecursionError in json.loads
        b"\xff\xfe\x00garbage",
    ],
)
def test_fetch_bad_root_is_bad_response(body):
    assert fetch_kind(body) == "bad_response"


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (403, "rate_limited"),
        (429, "rate_limited"),
        (500, "bad_response"),
        (404, "bad_response"),
        (204, "bad_response"),
    ],
)
def test_fetch_status(status, kind):
    assert fetch_kind(minified([item()]), status=status) == kind


def test_fetch_does_not_follow_a_redirect():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(301, headers={"Location": "https://evil.example/"})
        return httpx.Response(200, content=minified([item()]))

    with httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(ReleaseCheckError) as info:
            fetch_releases(client)
    assert info.value.kind == "bad_response"
    assert calls == [FEED_URL]


def test_fetch_transport_error_is_network():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with client_for(handler) as client:
        with pytest.raises(ReleaseCheckError) as info:
            fetch_releases(client)
    assert info.value.kind == "network"


class _Chunks(httpx.SyncByteStream):
    """A response body yielded in chunks, optionally with a pause before each."""

    def __init__(self, chunks: list[bytes], pause: float = 0.0) -> None:
        self.chunks = chunks
        self.pause = pause

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            if self.pause:
                time.sleep(self.pause)
            yield chunk


def test_fetch_declared_oversize_is_bad_response():
    body = b"[" + b" " * MAX_RESPONSE_BYTES + b"]"
    assert fetch_kind(body) == "bad_response"


def test_fetch_streamed_oversize_without_length_is_bad_response():
    chunk = b" " * (256 * 1024)
    count = MAX_RESPONSE_BYTES // len(chunk) + 2
    yielded: list[int] = []

    class Counting(_Chunks):
        def __iter__(self) -> Iterator[bytes]:
            for piece in super().__iter__():
                yielded.append(len(piece))
                yield piece

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Counting([b"["] + [chunk] * count + [b"]"]))

    with client_for(handler) as client:
        with pytest.raises(ReleaseCheckError) as info:
            fetch_releases(client)
    assert info.value.kind == "bad_response"
    assert len(yielded) < count  # aborted before the end


def test_fetch_gzip_bomb_counts_decompressed_bytes():
    inner = b"[" + b" " * (MAX_RESPONSE_BYTES + 1024) + b"]"
    packed = gzip.compress(inner)
    assert len(packed) < MAX_RESPONSE_BYTES // 100

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=packed, headers={"Content-Encoding": "gzip"})

    with client_for(handler) as client:
        with pytest.raises(ReleaseCheckError) as info:
            fetch_releases(client)
    assert info.value.kind == "bad_response"


def test_fetch_corrupt_gzip_is_bad_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"not gzip at all", headers={"Content-Encoding": "gzip"}
        )

    with client_for(handler) as client:
        with pytest.raises(ReleaseCheckError) as info:
            fetch_releases(client)
    assert info.value.kind == "bad_response"


def test_fetch_slow_drip_past_the_deadline_is_network(monkeypatch):
    monkeypatch.setattr(rel_mod, "TOTAL_DEADLINE_SEC", 0.05)
    body = minified([item()])
    pieces = [body[i : i + 4] for i in range(0, len(body), 4)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Chunks(pieces, pause=0.02))

    started = time.monotonic()
    with client_for(handler) as client:
        with pytest.raises(ReleaseCheckError) as info:
            fetch_releases(client)
    assert info.value.kind == "network"
    assert time.monotonic() - started < 2.0


def test_fetch_chunked_within_the_deadline_succeeds():
    body = minified([item("v2.2.1"), item("v2.2.0")])
    pieces = [body[i : i + 7] for i in range(0, len(body), 7)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Chunks(pieces))

    with client_for(handler) as client:
        assert [x.version for x in fetch_releases(client)] == [(2, 2, 1), (2, 2, 0)]


# --- 3. request hygiene -------------------------------------------------------


@pytest.mark.parametrize("ua", ["whygraph-cli", "whygraph-portal"])
def test_request_headers_are_exact(ua):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"[]")

    with build_client(ua, transport=httpx.MockTransport(handler)) as client:
        fetch_releases(client)
    (request,) = seen
    assert request.method == "GET"
    assert str(request.url) == FEED_URL
    assert request.content == b""
    headers = {k.lower(): v for k, v in request.headers.items()}
    assert headers["user-agent"] == ua
    assert headers["accept"] == "application/vnd.github+json"
    assert headers["x-github-api-version"] == "2022-11-28"
    assert set(headers) == {
        "host",
        "accept",
        "accept-encoding",
        "connection",
        "user-agent",
        "x-github-api-version",
    }
    assert "authorization" not in headers
    assert "cookie" not in headers


def test_build_client_settings():
    with build_client("whygraph-cli") as client:
        assert client.follow_redirects is False
        assert client.timeout == httpx.Timeout(10.0, connect=5.0)
        assert client.headers["User-Agent"] == "whygraph-cli"


def test_feed_constants():
    assert FEED_URL.endswith(f"per_page={PAGE_SIZE}")
    assert MAX_RESPONSE_BYTES == 2 * 1024 * 1024
    assert rel_mod.TOTAL_DEADLINE_SEC == 20.0


# --- 4. resolve ---------------------------------------------------------------


def test_resolve_grace_boundary():
    releases = [r("2.3.0", minutes_ago=59), r("2.2.1")]
    early = resolve(releases, (2, 2, 1), now=NOW, grace_sec=3600)
    assert early.status == "current"
    assert early.target.version == (2, 2, 1)

    releases = [r("2.3.0", minutes_ago=61), r("2.2.1")]
    late = resolve(releases, (2, 2, 1), now=NOW, grace_sec=3600)
    assert late.status == "update"
    assert late.target.version == (2, 3, 0)


def test_resolve_grace_is_read_time():
    releases = [r("2.3.0", minutes_ago=30), r("2.2.1")]
    assert resolve(releases, (2, 2, 1), now=NOW, grace_sec=3600).status == "current"
    later = NOW + timedelta(hours=2)
    assert resolve(releases, (2, 2, 1), now=later, grace_sec=3600).status == "update"


def test_resolve_without_now_takes_every_release():
    releases = [r("2.3.0", minutes_ago=1), r("2.2.1")]
    assert resolve(releases, (2, 2, 1)).target.version == (2, 3, 0)


def test_resolve_statuses():
    releases = [r("2.2.1"), r("2.2.0"), r("2.1.0")]
    assert resolve(releases, (2, 2, 1)) == Resolution(
        "current", releases[0], False, True
    )
    assert resolve(releases, (2, 2, 0)).status == "update"
    assert resolve(releases, (2, 2, 1), target=(2, 2, 1)).status == "current"
    down = resolve(releases, (2, 2, 1), target=(2, 1, 0))
    assert (down.status, down.target.version) == ("downgrade", (2, 1, 0))
    ahead = resolve(releases, (2, 3, 0))
    assert (ahead.status, ahead.target.version) == ("ahead", (2, 2, 1))


def test_resolve_compares_numerically():
    releases = [r("9.9.9"), r("10.0.0")]
    res = resolve(releases, (9, 9, 9))
    assert (res.status, res.target.version) == ("update", (10, 0, 0))


def test_resolve_explicit_target_older_than_newest():
    releases = [r("2.3.0"), r("2.2.1"), r("2.2.0")]
    res = resolve(releases, (2, 2, 0), target=(2, 2, 1))
    assert (res.status, res.target.version) == ("update", (2, 2, 1))


def test_resolve_unknown_explicit_target():
    releases = [r("2.2.1"), r("2.2.0")]
    assert resolve(releases, (2, 2, 0), target=(2, 2, 5)) == Resolution(
        "unknown", None, False, False
    )


def test_resolve_explicit_target_inside_grace_is_unknown():
    releases = [r("2.3.0", minutes_ago=10), r("2.2.1")]
    res = resolve(releases, (2, 2, 1), target=(2, 3, 0), now=NOW, grace_sec=3600)
    assert res == Resolution("unknown", None, False, False)


def test_resolve_no_release():
    assert resolve([], (2, 2, 1)) == Resolution("unknown", None, False, False)
    only_new = [r("2.3.0", minutes_ago=5)]
    assert resolve(only_new, (2, 2, 1), now=NOW, grace_sec=3600) == Resolution(
        "unknown", None, False, False
    )


def test_resolve_no_current():
    releases = [r("2.2.1"), r("2.2.0", manual=True)]
    res = resolve(releases, None)
    assert res.status == "unknown"
    assert res.target.version == (2, 2, 1)
    assert res.manual is True  # every release up to the target counts
    assert res.verified is False
    assert resolve([r("2.2.1")], None).manual is False


@pytest.mark.parametrize(
    ("marked", "expected"),
    [
        ("2.3.0", True),  # on the target
        ("2.2.5", True),  # on a release in between
        ("2.2.1", False),  # on current
        ("2.2.0", False),  # below current
        ("2.4.0", False),  # above an explicit target
    ],
)
def test_resolve_manual_marker(marked, expected):
    versions = ["2.4.0", "2.3.0", "2.2.5", "2.2.1", "2.2.0"]
    releases = [r(v, manual=(v == marked)) for v in versions]
    res = resolve(releases, (2, 2, 1), target=(2, 3, 0))
    assert res.status == "update"
    assert res.manual is expected


def test_resolve_marker_inside_grace_on_an_in_between_release_counts():
    # The target is the newest announced release; a marked in-between patch
    # that is still inside the grace hour is in the fetched list and counts.
    releases = [r("2.3.0"), r("2.2.2", minutes_ago=5, manual=True), r("2.2.1")]
    res = resolve(releases, (2, 2, 1), now=NOW, grace_sec=3600)
    assert (res.status, res.target.version, res.manual) == ("update", (2, 3, 0), True)


def test_resolve_marker_above_the_grace_target_is_ignored():
    releases = [r("2.4.0", minutes_ago=5, manual=True), r("2.3.0"), r("2.2.1")]
    res = resolve(releases, (2, 2, 1), now=NOW, grace_sec=3600)
    assert (res.target.version, res.manual) == ((2, 3, 0), False)


def _full_page(oldest_minor: int) -> list[Release]:
    return [r(f"3.{oldest_minor + i}.0") for i in range(PAGE_SIZE)]


def test_resolve_verified():
    # A list that reaches back to current is complete.
    assert resolve([r("2.3.0"), r("2.2.1")], (2, 2, 1)).verified is True
    # A short list that does not reach back is not proof: skipped items
    # (prereleases, drafts) shorten a page too.
    assert resolve([r("2.3.0"), r("2.2.1")], (2, 0, 0)).verified is False
    # A full page whose oldest release is newer than current may be truncated.
    page = _full_page(oldest_minor=1)
    behind = resolve(page, (2, 9, 0))
    assert (behind.status, behind.verified) == ("update", False)
    # A full page that reaches back to (or below) current is complete enough.
    assert resolve(page, (3, 1, 0)).verified is True
    assert resolve(page, (3, 5, 0)).verified is True
    # No current version is never verified.
    assert resolve([r("2.3.0")], None).verified is False


def test_resolve_does_not_need_sorted_input():
    releases = [r("2.2.0"), r("2.3.0"), r("2.2.1")]
    assert resolve(releases, (2, 2, 1)).target.version == (2, 3, 0)
