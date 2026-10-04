"""Unit tests for password hashing, the password rules and email normalization."""

from __future__ import annotations

import re
import threading
import unicodedata
from importlib import resources

import pytest
from argon2 import PasswordHasher

from whygraph.portal import passwords
from whygraph.portal.deps import ApiError
from whygraph.portal.passwords import (
    hash_password,
    normalize_email,
    validate_password,
    verify_password,
)

GOOD = "correct horse battery staple"


def test_round_trip_and_wrong_password():
    stored = hash_password(GOOD)
    assert stored.startswith("$argon2id$")
    assert verify_password(stored, GOOD) == (True, False)
    assert verify_password(stored, GOOD + "x") == (False, False)


def test_garbage_hash_is_a_failure_not_an_error():
    assert verify_password("not-a-hash", GOOD) == (False, False)


def test_rehash_flag_for_outdated_parameters():
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(GOOD)
    assert verify_password(weak, GOOD) == (True, True)
    assert verify_password(weak, "wrong") == (False, False)


def test_dummy_verify_for_unknown_user(monkeypatch):
    calls = []
    real = passwords._hasher

    class Spy:
        def verify(self, h, p):
            calls.append(h)
            return real.verify(h, p)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(passwords, "_hasher", Spy())
    assert verify_password(None, GOOD) == (False, False)
    assert len(calls) == 1 and calls[0].startswith("$argon2id$")


def test_nfc_forms_verify_the_same():
    composed = "café au lait, s'il vous plait"
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    stored = hash_password(composed)
    assert verify_password(stored, decomposed) == (True, False)
    assert verify_password(hash_password(decomposed), composed) == (True, False)


def test_at_most_four_hashes_at_once(monkeypatch):
    active = peak = 0
    lock = threading.Lock()
    gate = threading.Event()

    def slow_hash(self, pw):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        gate.wait(0.05)
        with lock:
            active -= 1
        return "h"

    class Slow:
        hash = slow_hash

    monkeypatch.setattr(passwords, "_hasher", Slow())
    threads = [threading.Thread(target=hash_password, args=("x",)) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert 1 <= peak <= 4


def _code(password, email="who@example.com"):
    with pytest.raises(ApiError) as exc:
        validate_password(password, email)
    assert exc.value.status == 422
    return exc.value.code


def test_length_rules():
    assert _code("a" * 14) == "weak_password"
    assert _code("") == "weak_password"
    assert _code("a" * 257) == "weak_password"
    validate_password("qZ7!" * 3 + "xyz", "who@example.com")  # 15
    validate_password("qZ7!" * 64, "who@example.com")  # 256


def test_length_counts_after_nfc():
    # 14 characters once composed, 15 when decomposed.
    decomposed = unicodedata.normalize("NFD", "é" * 14)
    assert len(decomposed) == 28
    assert _code(decomposed) == "weak_password"


def test_blocklisted_passphrase_refused():
    word = next(iter(passwords._blocklist()))
    assert _code(word) == "common_password"
    assert _code(word.upper()) == "common_password"


def test_email_and_local_part_refused():
    email = "a.very.long.local.part@example.com"
    assert _code(email, email) == "common_password"
    assert _code(email.upper(), email) == "common_password"
    local = "a.very.long.local.part"
    assert _code(local, email) == "common_password"
    validate_password(local + "-and-more", email)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  Alice@Example.COM ", "alice@example.com"),
        ("a@b", "a@b"),
    ],
)
def test_normalize_email(raw, expected):
    assert normalize_email(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "no-at",
        "@b.c",
        "a@",
        "a@@b.c",
        "a@b@c",
        "a b@c.d",
        "a@b .c",
        "x" * 253 + "@b.c",
    ],
)
def test_normalize_email_refusals(raw):
    with pytest.raises(ApiError) as exc:
        normalize_email(raw)
    assert exc.value.status == 422
    assert exc.value.code == "bad_email"


def test_email_length_limit_boundary():
    ok = "a" * 249 + "@b.co"
    assert len(ok) == 254
    assert normalize_email(ok) == ok
    with pytest.raises(ApiError):
        normalize_email("a" * 250 + "@b.co")


def test_blocklist_file():
    text = (
        resources.files("whygraph.portal")
        .joinpath("data/common_passwords.txt")
        .read_text(encoding="utf-8")
    )
    lines = text.splitlines()
    header = [line for line in lines if line.startswith("#")]
    assert any("SecLists" in line and "MIT" in line for line in header)
    assert any(
        "raw.githubusercontent.com/danielmiessler/SecLists" in line for line in header
    )
    entries = [line for line in lines if not line.startswith("#")]
    assert len(entries) > 1000
    assert entries == sorted(set(entries))
    assert all(e == e.lower() for e in entries)
    assert all(len(e) >= 15 for e in entries)
    assert passwords._blocklist() == frozenset(entries)
    assert not any(re.search(r"\s$", e) for e in entries)
