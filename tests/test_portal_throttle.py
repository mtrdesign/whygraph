"""Unit tests for the throttle, the client-IP key and the security event log."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from whygraph.portal.audit import audit, truncate_email
from whygraph.portal.throttle import Throttle, ip_key


@pytest.fixture
def records():
    """Capture the audit logger directly (other tests reconfigure logging)."""
    out: list[logging.LogRecord] = []

    class Grab(logging.Handler):
        def emit(self, record):
            out.append(record)

    handler = Grab(logging.INFO)
    audit_logger = logging.getLogger("whygraph.portal.audit")
    old_level, old_disabled = audit_logger.level, audit_logger.disabled
    audit_logger.addHandler(handler)
    audit_logger.setLevel(logging.INFO)
    audit_logger.disabled = False
    yield out
    audit_logger.removeHandler(handler)
    audit_logger.setLevel(old_level)
    audit_logger.disabled = old_disabled


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(limit=3, window=60, **kw):
    clock = Clock()
    return Throttle(limit, window, clock=clock, **kw), clock


def test_limit_and_retry_after():
    t, clock = make()
    for _ in range(3):
        assert t.check("k") is None
        t.record("k")
    assert t.check("k") == 60
    clock.now += 20
    assert t.check("k") == 40
    assert t.check("other") is None


def test_window_expiry():
    t, clock = make()
    for _ in range(3):
        t.record("k")
    clock.now += 60
    assert t.check("k") is None
    t.record("k")
    assert t.check("k") is None  # old events dropped, only 1 live


def test_check_does_not_count():
    t, _ = make(limit=1)
    for _ in range(10):
        assert t.check("k") is None
    t.record("k")
    assert t.check("k") is not None


def test_hit_checks_then_records():
    t, clock = make(limit=2)
    assert t.hit("k") is None
    assert t.hit("k") is None
    assert t.hit("k") == 60  # refused attempts are not counted
    clock.now += 61
    assert t.hit("k") is None


def test_retry_after_is_at_least_one_second():
    t, clock = make(limit=1, window=10)
    t.record("k")
    clock.now += 9.999
    assert t.check("k") == 1


def test_key_cap_evicts_oldest():
    t, _ = make(limit=1, max_keys=3)
    for k in "abcd":
        t.record(k)
    assert t.check("a") is None  # evicted
    for k in "bcd":
        assert t.check(k) is not None
    assert len(t._events) == 3


def test_ip_key():
    assert ip_key({"client": ("203.0.113.9", 5000)}) == "203.0.113.9"
    a = ip_key({"client": ("2001:db8:1:2:aaaa::1", 1)})
    b = ip_key({"client": ("2001:db8:1:2:bbbb::2", 1)})
    c = ip_key({"client": ("2001:db8:1:3::1", 1)})
    assert a == b == "2001:db8:1:2::/64"
    assert c != a
    assert ip_key({"client": ("::ffff:203.0.113.9", 1)}) == "203.0.113.9"
    assert ip_key({}) == "unknown"
    assert ip_key({"client": None}) == "unknown"


def test_truncate_email():
    assert truncate_email("alice@example.com") == "ali...@example.com"
    assert truncate_email("ab@example.com") == "ab...@example.com"
    assert truncate_email("") == ""
    assert truncate_email(None) == ""


def test_audit_record(records):
    request = SimpleNamespace(
        scope={
            "client": ("203.0.113.9", 1),
            "headers": [(b"host", b"acme.whygraph.localhost:8765")],
        }
    )
    audit("login_failure", request, uid="u1", target="u2", email="ali...@x.com")
    (record,) = records
    assert record.name == "whygraph.portal.audit"
    assert record.levelno == logging.INFO
    assert record.audit == {
        "event": "login_failure",
        "uid": "u1",
        "target": "u2",
        "ip": "203.0.113.9",
        "host": "acme.whygraph.localhost:8765",
        "email": "ali...@x.com",
    }
    assert "login_failure" in record.getMessage()


def test_audit_accepts_a_bare_scope(records):
    audit("logout", {"client": ("::1", 1), "headers": []})
    assert records[0].audit["ip"] == "::1"
