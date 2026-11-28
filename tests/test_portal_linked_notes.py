"""``LinkedProject``'s notes calls over the fake platform (plan 63 step 4)."""

from __future__ import annotations

import httpx
import pytest

from platform_fake import ORG, PLATFORM_ORIGIN, SLUG, TOKEN, FakePlatform
from whygraph.core.remote import RemoteError, RemoteNotFound
from whygraph.portal.linked import HOOK_TIMEOUT, LinkedProject
from whygraph.portal.platform_client import PlatformHttp, api_origin_for

API_ORIGIN = api_origin_for(PLATFORM_ORIGIN, ORG)
PAYLOAD = {"body": "Keep it.", "kind": "decision", "anchor": {"kind": "project"}}


def _make(fake: FakePlatform) -> LinkedProject:
    fake.add_token()
    return LinkedProject(
        PlatformHttp(
            platform_origin=PLATFORM_ORIGIN,
            api_origin=API_ORIGIN,
            token=TOKEN,
            transport=httpx.MockTransport(fake.handle),
        ),
        slug=SLUG,
        org=ORG,
        platform_origin=PLATFORM_ORIGIN,
    )


def _paths(fake: FakePlatform) -> list[str]:
    return [r.url.path for r in fake.requests]


def test_hook_timeout_sums_to_the_budget():
    t = HOOK_TIMEOUT
    assert t.connect + t.read + t.write + t.pool == pytest.approx(1.2)


def test_unsupported_platform_makes_no_notes_request():
    fake = FakePlatform(notes_supported=False)
    remote = _make(fake)
    with pytest.raises(RemoteError) as err:
        remote.propose_note(PAYLOAD)
    assert (err.value.status, err.value.reason) == ("ok", "note_unsupported")
    assert remote.notes_for("a.py") == ([], False)
    assert remote.project_notes() == ([], False)
    with pytest.raises(RemoteNotFound):
        remote.note(1)
    assert _paths(fake) == ["/api/v1/meta"]


def test_unknown_route_on_a_capable_platform_reads_as_unsupported():
    fake = FakePlatform()
    fake.notes_refusal = (401, "login_required")
    remote = _make(fake)
    assert remote.notes_for("a.py") == ([], False)
    assert remote.project_notes() == ([], False)
    with pytest.raises(RemoteNotFound):
        remote.note(1)


def test_propose_builds_the_review_url_locally():
    fake = FakePlatform()
    remote = _make(fake)
    out = remote.propose_note(PAYLOAD)
    assert out["review"] == f"{API_ORIGIN}/p/{SLUG}/notes?id=7"
    assert out["note_id"] == 7 and "project" not in out
    assert fake.proposals[0]["anchor"] == {"kind": "project"}


def test_reads_return_notes_and_supported():
    fake = FakePlatform()
    remote = _make(fake)
    notes, supported = remote.notes_for(
        "a.py", qualified_name="pkg.f", lines=(1, 3), timeout=HOOK_TIMEOUT
    )
    assert supported and notes[0]["id"] == 1
    assert remote.project_notes()[1] is True
    assert remote.note(1)["id"] == 1


def test_note_404_is_not_found_and_non_int_makes_no_request():
    fake = FakePlatform()
    remote = _make(fake)
    with pytest.raises(RemoteNotFound):
        remote.note(99)
    assert remote.status == "ok"
    before = len(fake.requests)
    for bad in (0, -1, True, "1"):
        with pytest.raises(RemoteNotFound):
            remote.note(bad)  # type: ignore[arg-type]
    assert len(fake.requests) == before


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (429, "note_limit"),
        (422, "note_invalid"),
        (409, "note_state"),
        (429, "throttled"),
        (403, "forbidden"),
    ],
)
def test_platform_refusals_keep_their_code_and_the_link_ok(status, code):
    fake = FakePlatform()
    remote = _make(fake)
    fake.notes_refusal = (status, code)
    with pytest.raises(RemoteError) as err:
        remote.propose_note(PAYLOAD)
    assert (err.value.status, err.value.reason) == ("ok", code)
    assert remote.status == "ok" and remote.reason == code


def test_a_slow_read_is_platform_slow_and_the_link_stays_ok():
    fake = FakePlatform()
    remote = _make(fake)
    remote._capabilities = frozenset({"notes"})
    fake.failure = "connect_timeout"
    with pytest.raises(RemoteError) as err:
        remote.notes_for("a.py", timeout=HOOK_TIMEOUT)
    assert (err.value.status, err.value.reason) == ("ok", "platform_slow")
