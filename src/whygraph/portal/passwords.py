"""Password hashing and the rules a password must meet.

Sync functions, called from sync handlers (FastAPI runs those in a thread
pool). Hashing uses argon2id with ``argon2-cffi``'s defaults, and a
module-level semaphore bounds how many hashes run at once so a login flood
cannot take 64 MiB per thread. The rules follow NIST 800-63B: 15-256
characters, not on a blocklist of common passwords, no composition rules.
"""

from __future__ import annotations

import threading
import unicodedata
from functools import lru_cache
from importlib import resources

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_LENGTH = 15
MAX_LENGTH = 256
MAX_EMAIL_LENGTH = 254

_hasher = PasswordHasher()
_slots = threading.BoundedSemaphore(4)
_dummy_lock = threading.Lock()
_dummy_hash: str | None = None


def _nfc(password: str) -> str:
    return unicodedata.normalize("NFC", password)


def hash_password(password: str) -> str:
    """Hash a password with argon2id.

    Parameters
    ----------
    password : str
        The plain password; NFC-normalized first.

    Returns
    -------
    str
        The encoded hash (parameters and salt included).
    """
    with _slots:
        return _hasher.hash(_nfc(password))


def _dummy() -> str:
    global _dummy_hash
    with _dummy_lock:
        if _dummy_hash is None:
            _dummy_hash = _hasher.hash("whygraph dummy password")
        return _dummy_hash


def verify_password(stored_hash: str | None, password: str) -> tuple[bool, bool]:
    """Check a password against a stored hash.

    Parameters
    ----------
    stored_hash : str or None
        The user's hash, or ``None`` for an unknown user: a dummy verify
        runs so both cases cost the same time.
    password : str
        The plain password; NFC-normalized first.

    Returns
    -------
    tuple of bool
        ``(ok, needs_rehash)``. ``needs_rehash`` is only ever true when
        ``ok`` is, and means the hash uses outdated parameters.
    """
    password = _nfc(password)
    with _slots:
        if stored_hash is None:
            try:
                _hasher.verify(_dummy(), password)
            except VerificationError:
                pass
            return False, False
        try:
            _hasher.verify(stored_hash, password)
        except (VerificationError, InvalidHashError):
            return False, False
        return True, _hasher.check_needs_rehash(stored_hash)


@lru_cache(maxsize=1)
def _blocklist() -> frozenset[str]:
    text = (
        resources.files("whygraph.portal")
        .joinpath("data/common_passwords.txt")
        .read_text(encoding="utf-8")
    )
    return frozenset(
        line
        for line in (raw.strip() for raw in text.splitlines())
        if line and not line.startswith("#")
    )


def is_common_password(password: str) -> bool:
    """Return whether ``password`` (case-insensitive) is on the blocklist."""
    return _nfc(password).lower() in _blocklist()


def normalize_email(email: str) -> str:
    """Return the login form of an email: trimmed and lower-cased.

    Parameters
    ----------
    email : str
        What the user typed.

    Returns
    -------
    str
        The normalized address.

    Raises
    ------
    ApiError
        422 ``bad_email`` unless it has exactly one ``@`` with text on both
        sides, no whitespace and at most 254 characters.
    """
    from .deps import ApiError  # lazy: deps imports modules that import this one

    value = (email or "").strip().lower()
    local, sep, domain = value.partition("@")
    if (
        not sep
        or not local
        or not domain
        or "@" in domain
        or len(value) > MAX_EMAIL_LENGTH
        or any(c.isspace() for c in value)
    ):
        raise ApiError(422, "enter a valid email address", code="bad_email")
    return value


def validate_password(password: str, email: str) -> None:
    """Refuse a password that breaks the rules.

    Parameters
    ----------
    password : str
        The candidate; NFC-normalized first.
    email : str
        The account's email, which the password may not equal (nor its
        local part), case-insensitively.

    Raises
    ------
    ApiError
        422 ``weak_password`` outside 15-256 characters; 422
        ``common_password`` for a blocklisted password or one equal to the
        email or its local part.
    """
    from .deps import ApiError  # lazy: deps imports modules that import this one

    password = _nfc(password)
    if not MIN_LENGTH <= len(password) <= MAX_LENGTH:
        raise ApiError(
            422,
            f"use {MIN_LENGTH} to {MAX_LENGTH} characters",
            code="weak_password",
        )
    lowered = password.lower()
    email = (email or "").strip().lower()
    if lowered in (email, email.partition("@")[0]) or lowered in _blocklist():
        raise ApiError(
            422, "that password is too common or guessable", code="common_password"
        )
