"""The project-transfer bundle: its wire format, writer and reader (plan 42 section 5.2).

A bundle is one ``.zip`` holding the knowledge a project has built up (notes,
purpose runs, rationale cards, LLM descriptions, optionally the exporter's
chats and the portable settings). Its layout and limits are plan 42 section
7.1; the wire models in this module are the format's documentation.

This module is **pure**: it touches no database, binds no project and holds
no portal state. It never extracts anything to disk - every member is read as
a stream from the open archive. The archive is untrusted input, so
:func:`read_bundle` works in a fixed order and trusts no header field:

0. the end-of-central-directory record is parsed with :mod:`struct` before
   :class:`zipfile.ZipFile` is ever built (a 100 MiB upload of empty entries
   would otherwise cost hundreds of MiB inside ``ZipFile()``);
1. the archive's names must be known, unique and plain;
2. every entry must be stored or deflated, unencrypted and not a symlink;
3. ``manifest.json`` is parsed, its version checked and every member's row
   count checked against its cap **before** that member is read;
4. a verification pass streams every member, counting the bytes actually
   read against the limits and checking each member's size, sha256 and row
   count - so a CRC error or a lie surfaces before any record is used;
5. :meth:`Bundle.iter_records` then parses the records lazily;
6. every failure of the stdlib or of Pydantic becomes ``400 bundle_invalid``
   naming the member and line only, never the offending value.

The sha256 values in the manifest are integrity checks, not authentication:
anyone can write a bundle that passes them.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import struct
import zipfile
import zlib
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Annotated, Any, BinaryIO, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
)

from whygraph.core.import_checks import SINGLE_LINE_BAD
from whygraph.serve.errors import ServeError

# ---------------------------------------------------------------------------
# Format constants and limits (plan 42 section 7.1)
# ---------------------------------------------------------------------------

FORMAT = "whygraph-project-bundle"
"""The ``format`` every manifest names."""

FORMAT_VERSION = 1
"""The one ``format_version`` this portal writes and reads (D8)."""

KIB = 1024
MIB = 1024 * KIB

MAX_BUNDLE_BYTES = 100 * MIB
"""The largest bundle file accepted or written."""

MAX_UNCOMPRESSED_BYTES = 400 * MIB
"""The most bytes the reader decompresses, counted as actually read."""

MAX_LINE_BYTES = 8 * MIB
"""The longest JSONL line."""

MAX_MANIFEST_BYTES = 64 * KIB
"""The largest ``manifest.json``."""

MAX_CONFIG_BYTES = 64 * KIB
"""The largest ``config.json``."""

MAX_CENTRAL_DIRECTORY_BYTES = 64 * KIB
"""The largest central directory the pre-check lets through to ``ZipFile``."""

CHUNK_BYTES = 64 * KIB
"""The read size for every member."""

MAX_INT = 2**31
"""The bound of every record integer: ``0 <= n <= MAX_INT``.

``config.json`` allows ``-MAX_INT <= n <= MAX_INT`` (its meaning is checked
by the importer's config validation); tool-call arguments allow ``|n| <=
2**53``.
"""

MAX_ROOT_COMMITS = 64
"""The most root commits a manifest may state."""

MAX_CONFIG_DEPTH = 8
"""The deepest nesting ``config.json`` may use."""

MANIFEST = "manifest.json"
ACTORS = "actors.jsonl"
NOTES = "notes.jsonl"
PURPOSE_RUNS = "purpose_runs.jsonl"
RATIONALE = "rationale.jsonl"
COMMIT_DESCRIPTIONS = "commit_descriptions.jsonl"
FILE_DESCRIPTIONS = "file_descriptions.jsonl"
CHAT_SESSIONS = "chat_sessions.jsonl"
CHAT_MESSAGES = "chat_messages.jsonl"
CONFIG = "config.json"

MEMBERS: tuple[str, ...] = (
    MANIFEST,
    ACTORS,
    NOTES,
    PURPOSE_RUNS,
    RATIONALE,
    COMMIT_DESCRIPTIONS,
    FILE_DESCRIPTIONS,
    CHAT_SESSIONS,
    CHAT_MESSAGES,
    CONFIG,
)
"""Every name an archive may hold, in the order the reader verifies them."""

CORE_MEMBERS: tuple[str, ...] = (
    ACTORS,
    NOTES,
    PURPOSE_RUNS,
    RATIONALE,
    COMMIT_DESCRIPTIONS,
    FILE_DESCRIPTIONS,
)
"""The members every bundle carries (possibly empty)."""

CHAT_MEMBERS: tuple[str, ...] = (CHAT_SESSIONS, CHAT_MESSAGES)
"""The members present exactly when ``includes.chats``."""

TRANSACTION_MEMBERS: tuple[str, ...] = (
    ACTORS,
    NOTES,
    PURPOSE_RUNS,
    CHAT_SESSIONS,
    CHAT_MESSAGES,
)
"""The members the import loads whole and writes in its one core transaction."""

MAX_TRANSACTION_BYTES = 48 * MIB
"""The most uncompressed bytes the :data:`TRANSACTION_MEMBERS` may hold together.

The row caps alone let 12,500 chat messages of 30 KiB each through (about
360 MiB), and the import parses those members whole into memory and writes
them under one write lock. Measured on an Apple-silicon laptop with every
transaction member at its row cap and the messages grown to reach the size
(plan 42 step 12's lock-time script), the apply's core transaction held the
lock 0.49 s at 9 MiB, 1.1-1.3 s at 33-53 MiB, 1.3 s at 59 MiB and 1.4-1.8 s
at 72 MiB, and the import's peak RSS rose by 40, 135-185, 235 and 270 MiB:
48 MiB keeps the lock well under the 2 s budget (plan 42 Q10) before the
steep part. Checked on the sizes the archive declares and on the bytes
actually read; the writer enforces it too, so an export never writes what an
import refuses."""

ROW_CAPS: dict[str, int] = {
    ACTORS: 10_000,
    NOTES: 5_000,
    PURPOSE_RUNS: 100,
    RATIONALE: 50_000,
    COMMIT_DESCRIPTIONS: 200_000,
    FILE_DESCRIPTIONS: 500_000,
    CHAT_SESSIONS: 500,
    CHAT_MESSAGES: 12_500,
    CONFIG: 1,
}
"""The most rows per member (``config.json`` is one document).

The members the import writes in its one core transaction (actors, notes,
purpose runs, chats) are capped so that transaction holds the SQLite write
lock well under the 2 s budget (plan 42 Q10, step 12's measurement). The
re-creatable caches (rationale cards, commit and file descriptions) are
written afterwards in batches of their own (Q11), so their caps bound the
bundle, not one lock."""

_EOCD_SIG = b"PK\x05\x06"
_EOCD_SIZE = 22
_ZIP64_LOCATOR_SIG = b"PK\x06\x07"
_ZIP64_LOCATOR_SIZE = 20
_LOCAL_HEADER_SIG = b"PK\x03\x04"
_LOCAL_HEADER_SIZE = 30
_MAX_COMMENT = 0xFFFF
_ZIP64_EXTRA_ID = 0x0001
_S_IFLNK = 0o120000

_SHA256_RE = r"^[0-9a-f]{64}$"
_SHA1_RE = r"^[0-9a-f]{40}$"

_STD_EXCEPTIONS = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    RuntimeError,
    NotImplementedError,
    zlib.error,
    EOFError,
    OSError,
    OverflowError,
    RecursionError,
    UnicodeDecodeError,
    ValueError,
    struct.error,
)
"""What the stdlib raises on a hostile archive; all of it is ``bundle_invalid``."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BundleError(ServeError):
    """A refusal of a bundle: ``400``, ``413`` or ``415`` with a code.

    Codes are ``bundle_invalid`` (``400``; ``415`` for a wrong content
    type, raised by the route), ``bundle_version`` (``400``) and
    ``bundle_too_large`` (``413``). The message names a member and a line at
    most; it never echoes a value from the bundle.

    Parameters
    ----------
    status : int
        The HTTP status.
    message : str
        The server's own sentence.
    code : str
        The machine-readable code.
    """


def _invalid(message: str) -> BundleError:
    """A ``400 bundle_invalid`` refusal."""
    return BundleError(400, f"Invalid bundle: {message}", code="bundle_invalid")


def _too_large(message: str) -> BundleError:
    """A ``413 bundle_too_large`` refusal."""
    return BundleError(413, f"Bundle too large: {message}", code="bundle_too_large")


_TRANSACTION_TOO_LARGE = (
    f"people, notes, purpose runs and chats are over {MAX_TRANSACTION_BYTES} "
    "bytes together"
)


def _whygraph_version() -> str:
    """This portal's WhyGraph version, or ``unknown`` in a bare checkout."""
    try:
        return _pkg_version("whygraph")
    except PackageNotFoundError:
        return "unknown"


# ---------------------------------------------------------------------------
# Wire models (plan 42 section 7.1, D7)
# ---------------------------------------------------------------------------

_WIRE = ConfigDict(extra="ignore", strict=True, frozen=True)

Int = Annotated[int, Field(ge=0, le=MAX_INT)]
"""Every integer on the wire."""


def _single_line(value: str, max_chars: int) -> str:
    """Refuse over-long text and what every single-line import field refuses.

    That is :data:`~whygraph.core.import_checks.SINGLE_LINE_BAD`: control
    characters, line and paragraph separators, bidi controls and marks, and
    lone surrogates.
    """
    if len(value) > max_chars:
        raise ValueError(f"longer than {max_chars} characters")
    if SINGLE_LINE_BAD.search(value):
        raise ValueError("control character")
    return value


def _printable(value: object, max_chars: int) -> str | None:
    """``value`` when it is a clean single-line string, else ``None``."""
    if not isinstance(value, str):
        return None
    try:
        return _single_line(value, max_chars)
    except ValueError:
        return None


def _line(max_chars: int) -> AfterValidator:
    """A single-line, length-capped format field."""
    return AfterValidator(lambda v: _single_line(v, max_chars))


def _iso_timestamp(value: str) -> str:
    """Refuse a value that is not an ISO-8601 timestamp."""
    _single_line(value, 40)
    datetime.fromisoformat(value)
    return value


def _distinct(values: list[str]) -> list[str]:
    """Refuse a list that repeats a value."""
    if len(set(values)) != len(values):
        raise ValueError("repeated value")
    return values


Label = Annotated[str, _line(200)]
Timestamp = Annotated[str, AfterValidator(_iso_timestamp)]
Sha256 = Annotated[str, Field(pattern=_SHA256_RE)]
Sha1 = Annotated[str, Field(pattern=_SHA1_RE)]


class _JsonCheck:
    """Walk a parsed JSON value iteratively and refuse what JSON cannot carry."""

    def __init__(self, *, max_depth: int | None, int_bound: int) -> None:
        self.max_depth = max_depth
        self.int_bound = int_bound

    def __call__(self, value: Any) -> Any:
        stack: list[tuple[Any, int]] = [(value, 1)]
        while stack:
            item, depth = stack.pop()
            if isinstance(item, dict | list):
                if self.max_depth is not None and depth > self.max_depth:
                    raise ValueError(f"nested deeper than {self.max_depth} levels")
                children = item.values() if isinstance(item, dict) else item
                stack.extend((child, depth + 1) for child in children)
            elif isinstance(item, bool) or item is None or isinstance(item, str):
                continue
            elif isinstance(item, int):
                if abs(item) > self.int_bound:
                    raise ValueError("integer out of range")
            elif isinstance(item, float):
                if not math.isfinite(item):
                    raise ValueError("non-finite number")
        return value


class MemberInfo(BaseModel):
    """One member's integrity record in the manifest.

    Attributes
    ----------
    sha256 : str
        Lowercase hex sha256 of the member's bytes.
    rows : int
        JSONL lines in the member (``1`` for ``config.json``).
    """

    model_config = _WIRE

    sha256: Sha256
    rows: Int


class ManifestSource(BaseModel):
    """Where a bundle was exported, as stated by the bundle.

    Attributes
    ----------
    mode : {"local", "production"}
        The exporting portal's mode.
    label : str
        ``<org>/<slug>``.
    name : str
        The project's display name.
    """

    model_config = _WIRE

    mode: Literal["local", "production"]
    label: Label
    name: Label


class ManifestRepo(BaseModel):
    """The repository a bundle belongs to, as stated by the bundle (F8).

    Attributes
    ----------
    root_commits : list of str
        1 to 64 distinct 40-hex root SHAs, oldest first. The import gate
        needs one of them on the target; it guards against accidents only.
    origin : str or None
        Advisory ``host/owner/name``.
    default_branch : str or None
        Advisory default branch name.
    """

    model_config = _WIRE

    root_commits: Annotated[
        list[Sha1],
        Field(min_length=1, max_length=MAX_ROOT_COMMITS),
        AfterValidator(_distinct),
    ]
    origin: Annotated[str, _line(300)] | None = None
    default_branch: Annotated[str, _line(255)] | None = None


class ManifestIncludes(BaseModel):
    """Which optional members the bundle carries.

    Attributes
    ----------
    chats : bool
        The two chat members are present exactly when this is true.
    config : bool
        ``config.json`` is present exactly when this is true.
    """

    model_config = _WIRE

    chats: bool = False
    config: bool = False


class Manifest(BaseModel):
    """``manifest.json``: what the bundle is and what it holds.

    Attributes
    ----------
    format : str
        Always :data:`FORMAT`.
    format_version : int
        Always :data:`FORMAT_VERSION`.
    whygraph_version : str
        The exporting portal's version (informational; named in
        ``bundle_version`` errors).
    schema_revision : str or None
        The exporting project database's Alembic revision (informational).
    exported_at : str
        ISO-8601 UTC export time.
    source : ManifestSource
        The exporting project, as stated by the bundle.
    repo : ManifestRepo
        The repository, as stated by the bundle.
    includes : ManifestIncludes
        The optional members.
    members : dict of str to MemberInfo
        Every member but ``manifest.json``; must equal the archive's set.
    """

    model_config = _WIRE

    format: Literal["whygraph-project-bundle"]
    format_version: Literal[1]
    whygraph_version: Annotated[str, _line(64)]
    schema_revision: Annotated[str, _line(64)] | None = None
    exported_at: Timestamp
    source: ManifestSource
    repo: ManifestRepo
    includes: ManifestIncludes = ManifestIncludes()
    members: dict[str, MemberInfo] = {}


class _ManifestHead(BaseModel):
    """Just enough of a manifest to tell its version, read leniently."""

    model_config = ConfigDict(extra="ignore")

    format: Any = None
    format_version: Any = None
    whygraph_version: Any = None


class ActorRecord(BaseModel):
    """``actors.jsonl``: a uid the bundle names, with a display name (D12).

    Attributes
    ----------
    uid : str
        The source portal's uid, unprefixed and non-empty.
    display_name : str
        The name to show beside the "imported" badge.
    """

    model_config = _WIRE

    uid: str
    display_name: str


class NoteRecord(BaseModel):
    """``notes.jsonl``: one ``project_note`` row in wire form (plan 42 section 5.3).

    Mirrors the table column for column, except ``proposed_body``: the wire
    carries only ``edited_on_confirm`` (F15). ``refs`` is the decoded list.
    Values are checked by :func:`whygraph.mcp.notes.import_notes`.
    """

    model_config = _WIRE

    id: Int
    kind: str
    body: str
    edited_on_confirm: bool = False
    status: str
    anchor_kind: str
    anchor_path: str | None = None
    line_start: Int | None = None
    line_end: Int | None = None
    qualified_name: str | None = None
    area: str | None = None
    content_hash: str | None = None
    source: str
    origin: str | None = None
    refs: list[str] | None = None
    supersedes_id: Int | None = None
    proposed_by: str
    proposed_at: str
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    reconfirmed_by: str | None = None
    reconfirmed_at: str | None = None
    retired_by: str | None = None
    retired_at: str | None = None
    retire_reason: str | None = None


class PurposeRunRecord(BaseModel):
    """``purpose_runs.jsonl``: one ``project_purpose_run`` row (plan 42 section 5.5).

    ``sources`` is the decoded list; ``note_id`` is the source note id,
    remapped on import.
    """

    model_config = _WIRE

    requested_by: str
    started_at: str
    finished_at: str
    status: str
    input_hash: str | None = None
    input_bytes: Int | None = None
    sources: list[str] | None = None
    provider: str | None = None
    model: str | None = None
    tokens_in: Int | None = None
    tokens_out: Int | None = None
    note_id: Int | None = None
    error_code: str | None = None


class RationaleRecord(BaseModel):
    """``rationale.jsonl``: one ``rationale_cache`` row (plan 42 section 5.5).

    The three list columns are decoded lists; ``import_id`` never travels.
    """

    model_config = _WIRE

    path: str
    line_start: Int
    line_end: Int
    provider: str
    model: str
    evidence_fingerprint: str
    cached_at: str
    purpose: str
    why: str
    constraints: list[str]
    tradeoffs: list[str]
    risks: list[str]
    input_tokens: Int | None = None
    output_tokens: Int | None = None
    actual_provider: str | None = None
    actual_model: str | None = None
    qualified_name: str | None = None
    deferred_shas: str = ""


class CommitDescriptionRecord(BaseModel):
    """``commit_descriptions.jsonl``: a commit's LLM description (never a stub, F4)."""

    model_config = _WIRE

    sha: Sha1
    llm_description: str
    llm_description_model: str


class FileDescriptionRecord(BaseModel):
    """``file_descriptions.jsonl``: one file's LLM description inside a commit."""

    model_config = _WIRE

    commit_sha: Sha1
    path: str
    llm_description: str
    llm_description_model: str


class ChatSessionRecord(BaseModel):
    """``chat_sessions.jsonl``: one of the exporter's chat sessions (plan 42 section 5.4).

    ``owner_uid`` never travels (the importer becomes the owner);
    ``origin_key`` is the session's round-trip identity (D6).
    """

    model_config = _WIRE

    id: Int
    title: str
    provider: str
    model: str
    created_at: str
    updated_at: str
    active_leaf_id: Int | None = None
    effort: str | None = None
    mode: str | None = None
    origin_key: Sha256


class ToolCallRecord(BaseModel):
    """One requested tool call inside an assistant message.

    ``arguments`` is a JSON object (``ToolCall.arguments: dict``); its depth
    and size caps are the chat import's (plan 42 section 5.4).
    """

    model_config = _WIRE

    id: str
    name: str
    arguments: Annotated[
        dict[str, JsonValue],
        AfterValidator(_JsonCheck(max_depth=None, int_bound=2**53)),
    ]


class ReplayRecord(BaseModel):
    """The replay payload: exactly ``{"reasoning_content": str}`` (F6).

    The one wire model that forbids extra keys: a replay is sent back to a
    provider verbatim, so an unknown key is a format violation, never
    ignored.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    reasoning_content: str


class ChatMessageRecord(BaseModel):
    """``chat_messages.jsonl``: one chat message (plan 42 section 5.4).

    The ids are the source's; ``session_id`` / ``parent_id`` are remapped on
    import. ``tool_calls`` and ``replay`` are typed and re-serialised;
    ``grounding`` is the rendered string the chat import re-renders. Ratings
    never travel (D-m6).
    """

    model_config = _WIRE

    id: Int
    session_id: Int
    parent_id: Int | None = None
    role: str
    content: str
    tool_calls: list[ToolCallRecord] = []
    tool_call_id: str | None = None
    is_error: bool | None = None
    input_tokens: Int | None = None
    output_tokens: Int | None = None
    provider: str | None = None
    model: str | None = None
    error: str | None = None
    replay: ReplayRecord | None = None
    finish_reason: str | None = None
    effort: str | None = None
    mode: str | None = None
    grounding: str | None = None
    reasoning: str | None = None
    created_at: str


def _config_key(value: Any) -> Any:
    """Refuse an unprintable or over-long key anywhere in ``config.json``."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                _single_line(key, 200)
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
    return value


ConfigDocument = TypeAdapter(
    Annotated[
        dict[str, JsonValue],
        AfterValidator(_JsonCheck(max_depth=MAX_CONFIG_DEPTH, int_bound=MAX_INT)),
        AfterValidator(_config_key),
    ]
)
"""``config.json``: a D15-filtered project config layer (checked later by the importer)."""

MEMBER_MODELS: dict[str, type[BaseModel]] = {
    ACTORS: ActorRecord,
    NOTES: NoteRecord,
    PURPOSE_RUNS: PurposeRunRecord,
    RATIONALE: RationaleRecord,
    COMMIT_DESCRIPTIONS: CommitDescriptionRecord,
    FILE_DESCRIPTIONS: FileDescriptionRecord,
    CHAT_SESSIONS: ChatSessionRecord,
    CHAT_MESSAGES: ChatMessageRecord,
}
"""The record model of every JSONL member."""


def _describe(exc: ValidationError, model: type[BaseModel] | None) -> str:
    """Name the failing field and error type, never the input value."""
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    if not errors:
        return "invalid"
    first = errors[0]
    loc = first.get("loc") or ()
    field = loc[0] if loc else None
    fields = model.model_fields if model is not None else {}
    where = f"field {field!r}: " if isinstance(field, str) and field in fields else ""
    kind = str(first.get("type", "invalid"))
    return f"{where}{kind}"


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class _MemberWriter:
    """Count, hash and cap the bytes of one member as they are written."""

    def __init__(
        self, stream: BinaryIO, name: str, budget: list[int], core: list[int]
    ) -> None:
        self.stream = stream
        self.name = name
        self.budget = budget
        self.core = core if name in TRANSACTION_MEMBERS else None
        self.digest = hashlib.sha256()
        self.rows = 0

    def write_line(self, data: bytes) -> None:
        if len(data) + 1 > MAX_LINE_BYTES:
            raise _too_large(f"a line of {self.name} is over {MAX_LINE_BYTES} bytes")
        self.rows += 1
        if self.rows > ROW_CAPS[self.name]:
            raise _too_large(f"{self.name} has more than {ROW_CAPS[self.name]} rows")
        self.write(data + b"\n")

    def write(self, data: bytes) -> None:
        self.budget[0] += len(data)
        if self.budget[0] > MAX_UNCOMPRESSED_BYTES:
            raise _too_large(f"over {MAX_UNCOMPRESSED_BYTES} bytes uncompressed")
        if self.core is not None:
            self.core[0] += len(data)
            if self.core[0] > MAX_TRANSACTION_BYTES:
                raise _too_large(_TRANSACTION_TOO_LARGE)
        self.digest.update(data)
        self.stream.write(data)


def _zip_info(name: str) -> zipfile.ZipInfo:
    """A deflated, plain-file entry for ``name``."""
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def _config_bytes(config: Mapping[str, Any]) -> bytes:
    """Validate ``config.json`` the way the reader will and serialise it."""
    try:
        checked = ConfigDocument.validate_python(dict(config), strict=True)
    except ValidationError as exc:
        raise _invalid(f"{CONFIG}: {_describe(exc, None)}") from None
    data = json.dumps(
        checked, ensure_ascii=False, allow_nan=False, sort_keys=True
    ).encode("utf-8")
    if len(data) > MAX_CONFIG_BYTES:
        raise _too_large(f"{CONFIG} is over {MAX_CONFIG_BYTES} bytes")
    return data


def write_bundle(
    path: str | os.PathLike[str],
    manifest: Manifest,
    members: Mapping[str, Iterable[BaseModel] | Mapping[str, Any]],
) -> Manifest:
    """Write a bundle to a new file at ``path``.

    Parameters
    ----------
    path : path-like
        Where to write. Created exclusively with mode ``0600`` and never
        through a symlink; removed again if writing fails.
    manifest : Manifest
        The manifest to write. Its ``members`` and ``includes`` are
        replaced by what is actually written.
    members : mapping
        Member name to records. Every name of :data:`CORE_MEMBERS` is
        required; the two :data:`CHAT_MEMBERS` come together or not at all;
        :data:`CONFIG` maps to the D15-filtered config layer. Each record
        must be an instance of the member's :data:`MEMBER_MODELS` model.

    Returns
    -------
    Manifest
        The manifest as written.

    Raises
    ------
    BundleError
        ``413 bundle_too_large`` past a row, line or size cap (including
        :data:`MAX_TRANSACTION_BYTES`); ``400 bundle_invalid`` for an
        unknown member, a missing core member,
        a record of the wrong model or an invalid config layer.
    """
    unknown = set(members) - set(MEMBERS[1:])
    if unknown:
        raise _invalid("unknown member name")
    missing = [name for name in CORE_MEMBERS if name not in members]
    if missing:
        raise _invalid(f"missing member {missing[0]}")
    chats = [name in members for name in CHAT_MEMBERS]
    if any(chats) and not all(chats):
        raise _invalid("chat sessions and messages must travel together")
    config = members.get(CONFIG)
    if config is not None and not isinstance(config, Mapping):
        raise _invalid(f"{CONFIG} must be a mapping")
    config_data = _config_bytes(config) if config is not None else None

    target = os.fspath(path)
    fd = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    infos: dict[str, MemberInfo] = {}
    budget = [0]
    core = [0]
    try:
        with (
            os.fdopen(fd, "wb") as raw,
            zipfile.ZipFile(raw, "w", allowZip64=False) as zf,
        ):
            for name in MEMBERS[1:]:
                if name not in members:
                    continue
                with zf.open(_zip_info(name), "w") as stream:
                    writer = _MemberWriter(stream, name, budget, core)
                    if name == CONFIG and config_data is not None:
                        writer.rows = 1
                        writer.write(config_data)
                    else:
                        model = MEMBER_MODELS[name]
                        for record in members[name]:  # type: ignore[union-attr]
                            if not isinstance(record, model):
                                raise _invalid(
                                    f"{name} holds a record of the wrong type"
                                )
                            writer.write_line(record.model_dump_json().encode("utf-8"))
                infos[name] = MemberInfo(
                    sha256=writer.digest.hexdigest(), rows=writer.rows
                )
            final = manifest.model_copy(
                update={
                    "members": infos,
                    "includes": ManifestIncludes(
                        chats=all(chats), config=config is not None
                    ),
                }
            )
            data = final.model_dump_json().encode("utf-8")
            if len(data) > MAX_MANIFEST_BYTES:
                raise _too_large(f"{MANIFEST} is over {MAX_MANIFEST_BYTES} bytes")
            zf.writestr(_zip_info(MANIFEST), data)
    except BaseException:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise
    return final


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def _check_eocd(fh: BinaryIO, size: int) -> int:
    """Step 0: parse the end-of-central-directory record; return its entry count."""
    if size < _EOCD_SIZE:
        raise _invalid("not a zip archive")
    tail_len = min(size, _EOCD_SIZE + _MAX_COMMENT)
    fh.seek(size - tail_len)
    tail = fh.read(tail_len)
    pos = tail.rfind(_EOCD_SIG)
    if pos < 0 or len(tail) - pos < _EOCD_SIZE:
        raise _invalid("not a zip archive")
    (
        _sig,
        disk,
        cd_disk,
        disk_entries,
        total_entries,
        cd_size,
        cd_offset,
        comment_len,
    ) = struct.unpack("<4s4H2LH", tail[pos : pos + _EOCD_SIZE])
    eocd_offset = size - tail_len + pos
    if comment_len != 0 or eocd_offset + _EOCD_SIZE != size:
        raise _invalid("the archive has a comment or trailing data")
    if eocd_offset >= _ZIP64_LOCATOR_SIZE:
        fh.seek(eocd_offset - _ZIP64_LOCATOR_SIZE)
        if fh.read(4) == _ZIP64_LOCATOR_SIG:
            raise _invalid("ZIP64 archives are not accepted")
    if 0xFFFF in (disk_entries, total_entries) or 0xFFFFFFFF in (cd_size, cd_offset):
        raise _invalid("ZIP64 archives are not accepted")
    if disk != 0 or cd_disk != 0 or disk_entries != total_entries:
        raise _invalid("multi-disk archives are not accepted")
    if total_entries > len(MEMBERS):
        raise _invalid(f"more than {len(MEMBERS)} entries")
    if cd_size > MAX_CENTRAL_DIRECTORY_BYTES:
        raise _invalid("the central directory is too large")
    if cd_offset + cd_size != eocd_offset:
        raise _invalid("the central directory is not where the archive says")
    fh.seek(0)
    if total_entries and fh.read(4) != _LOCAL_HEADER_SIG:
        raise _invalid("the archive does not start with an entry")
    return total_entries


def _has_zip64_extra(extra: bytes) -> bool:
    """Whether an entry's extra field carries a ZIP64 record."""
    pos = 0
    while pos + 4 <= len(extra):
        header_id, length = struct.unpack("<HH", extra[pos : pos + 4])
        if header_id == _ZIP64_EXTRA_ID:
            return True
        pos += 4 + length
    return False


def _check_entries(
    zf: zipfile.ZipFile, total_entries: int
) -> dict[str, zipfile.ZipInfo]:
    """Steps 1-2: the names and per-entry rules; return the entries by name."""
    infos = zf.infolist()
    if len(infos) != total_entries:
        raise _invalid("the entry count does not match the central directory")
    names = [info.filename for info in infos]
    if len(set(names)) != len(names):
        raise _invalid("a member name repeats")
    for name in names:
        if (
            "/" in name
            or "\\" in name
            or ".." in name
            or name.startswith(".")
            or name not in MEMBERS
        ):
            raise _invalid("unknown member name")
    if MANIFEST not in names:
        raise _invalid(f"no {MANIFEST}")
    declared = 0
    for info in infos:
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise _invalid(f"{info.filename}: unsupported compression")
        if info.flag_bits & 0x1:
            raise _invalid(f"{info.filename}: encrypted members are not accepted")
        if (info.external_attr >> 16) & 0o170000 == _S_IFLNK:
            raise _invalid(f"{info.filename}: symlinks are not accepted")
        if _has_zip64_extra(info.extra):
            raise _invalid("ZIP64 archives are not accepted")
        declared += info.file_size
    if declared > MAX_UNCOMPRESSED_BYTES:
        raise _too_large(f"over {MAX_UNCOMPRESSED_BYTES} bytes uncompressed")
    core = sum(i.file_size for i in infos if i.filename in TRANSACTION_MEMBERS)
    if core > MAX_TRANSACTION_BYTES:
        raise _too_large(_TRANSACTION_TOO_LARGE)
    ordered = sorted(infos, key=lambda i: i.header_offset)
    if ordered and ordered[0].header_offset != 0:
        raise _invalid("the archive does not start with an entry")
    for before, after in zip(ordered, ordered[1:], strict=False):
        if after.header_offset < (
            before.header_offset + _LOCAL_HEADER_SIZE + before.compress_size
        ):
            raise _invalid("entries overlap")
    return {info.filename: info for info in infos}


def _read_small(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    """Read a whole small member, refusing past ``limit`` bytes actually read."""
    if info.file_size > limit:
        raise _invalid(f"{info.filename} is over {limit} bytes")
    with zf.open(info) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise _invalid(f"{info.filename} is over {limit} bytes")
    if len(data) != info.file_size:
        raise _invalid(f"{info.filename}: size does not match its header")
    return data


def _parse_manifest(data: bytes) -> Manifest:
    """Step 3: the version first (``bundle_version``), then the whole manifest."""
    try:
        head = _ManifestHead.model_validate_json(data)
    except ValidationError:
        raise _invalid(f"{MANIFEST} is not a JSON object") from None
    if (
        head.format != FORMAT
        or head.format_version != FORMAT_VERSION
        or (isinstance(head.format_version, bool))
    ):
        stated = _printable(head.whygraph_version, 64) or "an unknown version"
        raise BundleError(
            400,
            f"This bundle was written by WhyGraph {stated} in a format this "
            f"portal (WhyGraph {_whygraph_version()}) cannot read; it reads "
            f"{FORMAT} version {FORMAT_VERSION}.",
            code="bundle_version",
        )
    try:
        return Manifest.model_validate_json(data)
    except ValidationError as exc:
        raise _invalid(f"{MANIFEST}: {_describe(exc, Manifest)}") from None


def _check_manifest_members(manifest: Manifest, names: Iterable[str]) -> None:
    """Step 3: the declared members against the archive, the includes and the caps."""
    declared = set(manifest.members)
    if not declared <= set(MEMBERS[1:]):
        raise _invalid(f"{MANIFEST} names an unknown member")
    if {MANIFEST} | declared != set(names):
        raise _invalid(f"the archive's members do not match {MANIFEST}")
    for name in CORE_MEMBERS:
        if name not in declared:
            raise _invalid(f"no {name}")
    for name in CHAT_MEMBERS:
        if (name in declared) != manifest.includes.chats:
            raise _invalid(f"{name} does not match includes.chats")
    if (CONFIG in declared) != manifest.includes.config:
        raise _invalid(f"{CONFIG} does not match includes.config")
    for name, info in manifest.members.items():
        if name == CONFIG and info.rows != 1:
            raise _invalid(f"{CONFIG} must declare one row")
        if info.rows > ROW_CAPS[name]:
            raise _too_large(f"{name} has more than {ROW_CAPS[name]} rows")


def _chunks(
    stream: BinaryIO, total: list[int] | None = None, core: list[int] | None = None
) -> Iterator[bytes]:
    """Yield ``stream`` in :data:`CHUNK_BYTES` pieces, counting against the caps.

    ``total`` counts toward :data:`MAX_UNCOMPRESSED_BYTES`; ``core``, given
    for a :data:`TRANSACTION_MEMBERS` member, toward
    :data:`MAX_TRANSACTION_BYTES`.
    """
    while True:
        chunk = stream.read(CHUNK_BYTES)
        if not chunk:
            return
        if total is not None:
            total[0] += len(chunk)
            if total[0] > MAX_UNCOMPRESSED_BYTES:
                raise _too_large(f"over {MAX_UNCOMPRESSED_BYTES} bytes uncompressed")
        if core is not None:
            core[0] += len(chunk)
            if core[0] > MAX_TRANSACTION_BYTES:
                raise _too_large(_TRANSACTION_TOO_LARGE)
        yield chunk


def _lines(name: str, chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Split JSONL into lines, refusing a long, blank or unterminated one."""
    buffer = bytearray()
    for chunk in chunks:
        start = len(buffer)
        buffer += chunk
        searched = 0
        cut = buffer.find(b"\n", start)
        while cut >= 0:
            line = bytes(buffer[searched:cut])
            if len(line) + 1 > MAX_LINE_BYTES:
                raise _too_large(f"a line of {name} is over {MAX_LINE_BYTES} bytes")
            yield line
            searched = cut + 1
            cut = buffer.find(b"\n", searched)
        del buffer[:searched]
        if len(buffer) + 1 > MAX_LINE_BYTES:
            raise _too_large(f"a line of {name} is over {MAX_LINE_BYTES} bytes")
    if buffer:
        raise _invalid(f"{name}: the last line is not terminated")


class Bundle:
    """An opened, verified bundle; read its records with :meth:`iter_records`.

    Built by :func:`read_bundle` only, after every member has passed the
    verification pass. Holds the bundle file open (so later reads see the
    bytes that were verified) until :meth:`close`; use it as a context
    manager.

    Attributes
    ----------
    manifest : Manifest
        The parsed manifest.
    sha256 : str
        sha256 of the whole bundle file.
    size : int
        The bundle file's size in bytes.
    config : dict or None
        The parsed ``config.json``, or ``None`` when the bundle has none.
    """

    def __init__(
        self,
        fh: BinaryIO,
        zf: zipfile.ZipFile,
        manifest: Manifest,
        sha256: str,
        size: int,
        config: dict[str, Any] | None,
    ) -> None:
        self._fh = fh
        self._zf = zf
        self.manifest = manifest
        self.sha256 = sha256
        self.size = size
        self.config = config

    def rows(self, name: str) -> int:
        """The verified row count of ``name`` (``0`` when absent).

        Parameters
        ----------
        name : str
            A member name.

        Returns
        -------
        int
            Its rows.
        """
        info = self.manifest.members.get(name)
        return info.rows if info is not None else 0

    def iter_records(self, name: str) -> Iterator[tuple[int, BaseModel]]:
        """Parse a JSONL member lazily, one record per line.

        Parameters
        ----------
        name : str
            A member of :data:`MEMBER_MODELS`. An absent optional member
            yields nothing.

        Yields
        ------
        tuple of (int, BaseModel)
            The 1-based line number and the record, an instance of the
            member's model.

        Raises
        ------
        BundleError
            ``400 bundle_invalid`` naming the member and line of the first
            record that does not parse.
        """
        model = MEMBER_MODELS[name]
        if name not in self.manifest.members:
            return
        line_no = 0
        try:
            with self._zf.open(name) as stream:
                for line_no, line in enumerate(_lines(name, _chunks(stream)), 1):
                    try:
                        record = model.model_validate_json(line)
                    except ValidationError as exc:
                        raise _invalid(
                            f"{name} line {line_no}: {_describe(exc, model)}"
                        ) from None
                    yield line_no, record
        except BundleError:
            raise
        except _STD_EXCEPTIONS:
            raise _invalid(f"{name} line {line_no + 1}: unreadable") from None

    def close(self) -> None:
        """Close the archive and the file."""
        try:
            self._zf.close()
        finally:
            self._fh.close()

    def __enter__(self) -> Bundle:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _verify(
    zf: zipfile.ZipFile, infos: dict[str, zipfile.ZipInfo], manifest: Manifest
) -> dict[str, Any] | None:
    """Step 4: stream every member; return the parsed config, if any."""
    total = [infos[MANIFEST].file_size]
    core = [0]
    parsed: dict[str, Any] | None = None
    for name in MEMBERS[1:]:
        if name not in manifest.members:
            continue
        try:
            config = _verify_member(
                zf,
                infos[name],
                manifest.members[name],
                total,
                core if name in TRANSACTION_MEMBERS else None,
            )
        except BundleError:
            raise
        except _STD_EXCEPTIONS:
            raise _invalid(f"{name} cannot be read") from None
        if config is not None:
            parsed = config
    return parsed


def _verify_member(
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    expected: MemberInfo,
    total: list[int],
    core: list[int] | None = None,
) -> dict[str, Any] | None:
    """Step 4 for one member; return the parsed ``config.json``, else ``None``."""
    name = info.filename
    digest = hashlib.sha256()
    read = 0
    rows = 0
    if name == CONFIG:
        data = _read_small(zf, info, MAX_CONFIG_BYTES)
        total[0] += len(data)
        digest.update(data)
        read = len(data)
        rows = 1
    else:
        with zf.open(info) as stream:

            def counted(stream: BinaryIO = stream) -> Iterator[bytes]:
                nonlocal read
                for chunk in _chunks(stream, total, core):
                    read += len(chunk)
                    digest.update(chunk)
                    yield chunk

            for line in _lines(name, counted()):
                if not line.strip():
                    raise _invalid(f"{name} line {rows + 1}: blank line")
                rows += 1
                if rows > expected.rows:
                    raise _invalid(f"{name}: more rows than {MANIFEST} states")
    if read != info.file_size:
        raise _invalid(f"{name}: size does not match its header")
    if digest.hexdigest() != expected.sha256:
        raise _invalid(f"{name}: sha256 does not match {MANIFEST}")
    if rows != expected.rows:
        raise _invalid(f"{name}: row count does not match {MANIFEST}")
    if name != CONFIG:
        return None
    try:
        return ConfigDocument.validate_json(data)
    except ValidationError as exc:
        raise _invalid(f"{CONFIG}: {_describe(exc, None)}") from None


def read_bundle(path: str | os.PathLike[str]) -> Bundle:
    """Open, check and verify a bundle file (plan 42 section 5.2, steps 0-4).

    Parameters
    ----------
    path : path-like
        The uploaded bundle. Opened without following a final symlink;
        nothing is extracted.

    Returns
    -------
    Bundle
        The verified bundle, open; close it (or use it as a context
        manager) when done.

    Raises
    ------
    BundleError
        ``400 bundle_invalid`` for anything malformed, hostile or
        inconsistent; ``400 bundle_version`` for another ``format`` /
        ``format_version``; ``413 bundle_too_large`` past a size or row cap.
    """
    try:
        fd = os.open(os.fspath(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise _invalid("the bundle file cannot be opened") from None
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise _invalid("the bundle is not a regular file")
    fh = os.fdopen(fd, "rb")
    zf: zipfile.ZipFile | None = None
    try:
        size = st.st_size
        if size > MAX_BUNDLE_BYTES:
            raise _too_large(f"the file is over {MAX_BUNDLE_BYTES} bytes")
        file_digest = hashlib.sha256()
        fh.seek(0)
        for chunk in iter(lambda: fh.read(CHUNK_BYTES), b""):
            file_digest.update(chunk)
        total_entries = _check_eocd(fh, size)
        fh.seek(0)
        zf = zipfile.ZipFile(fh, "r")
        infos = _check_entries(zf, total_entries)
        manifest = _parse_manifest(_read_small(zf, infos[MANIFEST], MAX_MANIFEST_BYTES))
        _check_manifest_members(manifest, infos)
        config = _verify(zf, infos, manifest)
        return Bundle(fh, zf, manifest, file_digest.hexdigest(), size, config)
    except BaseException as exc:
        if zf is not None:
            zf.close()
        fh.close()
        if isinstance(exc, BundleError):
            raise
        if isinstance(exc, _STD_EXCEPTIONS):
            raise _invalid("the archive cannot be read") from None
        raise


__all__ = [
    "ACTORS",
    "CHAT_MEMBERS",
    "CHAT_MESSAGES",
    "CHAT_SESSIONS",
    "COMMIT_DESCRIPTIONS",
    "CONFIG",
    "CORE_MEMBERS",
    "FILE_DESCRIPTIONS",
    "FORMAT",
    "FORMAT_VERSION",
    "MANIFEST",
    "MAX_BUNDLE_BYTES",
    "MAX_CENTRAL_DIRECTORY_BYTES",
    "MAX_CONFIG_BYTES",
    "MAX_CONFIG_DEPTH",
    "MAX_INT",
    "MAX_LINE_BYTES",
    "MAX_MANIFEST_BYTES",
    "MAX_ROOT_COMMITS",
    "MAX_TRANSACTION_BYTES",
    "MAX_UNCOMPRESSED_BYTES",
    "MEMBERS",
    "MEMBER_MODELS",
    "NOTES",
    "PURPOSE_RUNS",
    "RATIONALE",
    "ROW_CAPS",
    "TRANSACTION_MEMBERS",
    "ActorRecord",
    "Bundle",
    "BundleError",
    "ChatMessageRecord",
    "ChatSessionRecord",
    "CommitDescriptionRecord",
    "ConfigDocument",
    "FileDescriptionRecord",
    "Manifest",
    "ManifestIncludes",
    "ManifestRepo",
    "ManifestSource",
    "MemberInfo",
    "NoteRecord",
    "PurposeRunRecord",
    "RationaleRecord",
    "ReplayRecord",
    "ToolCallRecord",
    "read_bundle",
    "write_bundle",
]
