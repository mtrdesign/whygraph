"""Encrypted secrets: the Fernet key file, the keyring, and the secret store.

Secrets (LLM API keys and GitHub tokens) live in the portal DB's
``secrets`` table as Fernet tokens; no API ever returns plaintext -
:func:`secret_status` gives ``{"set": True, "hint": "...a1b2"}``.

Honest scope: the key lives in ``<data dir>/secret.key``, beside the
database, so this protects a **copied or exported DB file**, not a
compromised data directory.

The key file is created race-free: written to
``secret.key.<pid>.tmp`` with ``O_CREAT | O_EXCL`` and mode 0600, fsynced,
then published with :func:`os.link`. ``EEXIST`` means another process won,
so the loser removes its temp file and reads the winner's complete key.
The file is never world-readable, even briefly, and never observed
half-written. Every consumer goes through :func:`load_keyring`, which
returns a :class:`cryptography.fernet.MultiFernet` (one key in M1), so a
later milestone can add an external key and rotate without restructuring.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlmodel import Session, select

from .db import data_dir
from .models import Secret, _now

KEY_FILE_NAME = "secret.key"

LLM_KEY_PROVIDERS: tuple[str, ...] = (
    "anthropic",
    "openai",
    "deepseek",
    "openrouter",
    "claude-cli",
)
"""Provider tags that take an API key (``ollama`` has none)."""

LLM_API_KEY = "llm_api_key"
GITHUB_TOKEN = "github_token"


# ---------------------------------------------------------------------------
# Key file + keyring
# ---------------------------------------------------------------------------


def key_path() -> Path:
    """Return the path of ``secret.key`` inside the data directory."""
    return data_dir() / KEY_FILE_NAME


def ensure_key() -> bytes:
    """Return the Fernet key, creating the key file on first use.

    Safe to call from several processes at once: exactly one key is ever
    published, and every caller returns that same key.

    Returns
    -------
    bytes
        The url-safe base64 Fernet key stored in ``secret.key``.
    """
    path = key_path()
    try:
        return _read_key(path)
    except FileNotFoundError:
        pass

    key = Fernet.generate_key()
    tmp = path.with_name(f"{KEY_FILE_NAME}.{os.getpid()}.tmp")
    fd = _open_exclusive(tmp)
    try:
        try:
            os.write(fd, key)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return _read_key(path)
        return key
    finally:
        tmp.unlink(missing_ok=True)


def _open_exclusive(tmp: Path) -> int:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        return os.open(tmp, flags, 0o600)
    except FileExistsError:
        # A leftover from a crashed process that had our pid.
        tmp.unlink(missing_ok=True)
        return os.open(tmp, flags, 0o600)


def _read_key(path: Path) -> bytes:
    key = path.read_bytes().strip()
    Fernet(key)  # validates: raises ValueError on a corrupt key file
    return key


def load_keyring() -> MultiFernet:
    """Load the keyring every encrypt / decrypt goes through.

    Returns
    -------
    MultiFernet
        Encrypts with the first key, decrypts with any. M1 holds exactly
        one (the key file); M2 can append an external key and rotate.
    """
    return MultiFernet([Fernet(ensure_key())])


def encrypt(plaintext: str) -> str:
    """Encrypt ``plaintext`` with the keyring; the token differs per call."""
    return load_keyring().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Decrypt a token from :func:`encrypt`.

    Raises
    ------
    cryptography.fernet.InvalidToken
        If no key in the keyring can open it (for example the key file
        was replaced). Callers treat that as "unreadable, re-enter".
    """
    return load_keyring().decrypt(ciphertext.encode("ascii")).decode("utf-8")


def hint_for(value: str) -> str:
    """Return the display hint for a secret: an ellipsis plus its last four chars."""
    return "…" + value[-4:]


# ---------------------------------------------------------------------------
# Secret store
# ---------------------------------------------------------------------------


def _check_scope(kind: str, provider: str | None) -> None:
    if kind == LLM_API_KEY:
        if provider not in LLM_KEY_PROVIDERS:
            raise ValueError(
                f"unknown LLM key provider {provider!r}; expected one of "
                f"{LLM_KEY_PROVIDERS}"
            )
    elif kind == GITHUB_TOKEN:
        if provider is not None:
            raise ValueError("a github_token has no provider")
    else:
        raise ValueError(f"unknown secret kind {kind!r}")


def _find(
    session: Session, kind: str, provider: str | None, project_id: int | None
) -> Secret | None:
    stmt = select(Secret).where(Secret.kind == kind)
    stmt = stmt.where(
        Secret.provider == provider if provider else Secret.provider.is_(None)
    )  # type: ignore[union-attr]
    stmt = stmt.where(
        Secret.project_id.is_(None)  # type: ignore[union-attr]
        if project_id is None
        else Secret.project_id == project_id
    )
    return session.exec(stmt).first()


def put_secret(
    session: Session,
    *,
    kind: str,
    value: str,
    provider: str | None = None,
    project_id: int | None = None,
) -> None:
    """Store (or replace) one secret, encrypted.

    One row per ``(scope, kind, provider)``: saving twice replaces the
    value in place, never adds a row. The caller commits and, if it holds
    a :class:`whygraph.portal.context.ContextCache`, invalidates it.

    Parameters
    ----------
    session : Session
        A portal DB session.
    kind : str
        ``"llm_api_key"`` or ``"github_token"``.
    value : str
        The plaintext; must be non-empty after stripping.
    provider : str, optional
        Provider tag, required for ``llm_api_key`` (see
        :data:`LLM_KEY_PROVIDERS`), forbidden for ``github_token``.
    project_id : int, optional
        ``None`` (default) stores the global default.

    Raises
    ------
    ValueError
        On an unknown kind / provider or an empty value.
    """
    _check_scope(kind, provider)
    value = value.strip()
    if not value:
        raise ValueError("secret value must not be empty")
    row = _find(session, kind, provider, project_id)
    if row is None:
        row = Secret(
            project_id=project_id, kind=kind, provider=provider, ciphertext="", hint=""
        )
        session.add(row)
    row.ciphertext = encrypt(value)
    row.hint = hint_for(value)
    row.created_at = _now()
    session.flush()


def delete_secret(
    session: Session,
    *,
    kind: str,
    provider: str | None = None,
    project_id: int | None = None,
) -> bool:
    """Delete one secret; return whether a row existed."""
    _check_scope(kind, provider)
    row = _find(session, kind, provider, project_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def read_secret(
    session: Session,
    *,
    kind: str,
    provider: str | None = None,
    project_id: int | None = None,
) -> str | None:
    """Return the decrypted secret, or ``None`` when none is stored.

    In-process use only (context build, credential injection) - never
    hand the result to an API response.

    Raises
    ------
    cryptography.fernet.InvalidToken
        If the stored token cannot be decrypted.
    """
    _check_scope(kind, provider)
    row = _find(session, kind, provider, project_id)
    return None if row is None else decrypt(row.ciphertext)


def secret_status(
    session: Session,
    *,
    kind: str,
    provider: str | None = None,
    project_id: int | None = None,
) -> dict[str, Any]:
    """Return the API-safe view of one secret.

    Returns
    -------
    dict
        ``{"set": False, "hint": None}`` when absent, else
        ``{"set": True, "hint": "...a1b2"}``. A secret that no longer
        decrypts gains ``"unreadable": True`` ("re-enter it"); the key
        appears only then. Never contains the value or the ciphertext.
    """
    _check_scope(kind, provider)
    row = _find(session, kind, provider, project_id)
    if row is None:
        return {"set": False, "hint": None}
    status: dict[str, Any] = {"set": True, "hint": row.hint}
    try:
        decrypt(row.ciphertext)
    except InvalidToken:
        status["unreadable"] = True
    return status


__all__ = [
    "GITHUB_TOKEN",
    "InvalidToken",
    "KEY_FILE_NAME",
    "LLM_API_KEY",
    "LLM_KEY_PROVIDERS",
    "decrypt",
    "delete_secret",
    "encrypt",
    "ensure_key",
    "hint_for",
    "key_path",
    "load_keyring",
    "put_secret",
    "read_secret",
    "secret_status",
]
