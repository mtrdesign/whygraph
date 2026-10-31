"""Project purpose: the short "what is this project for" card.

This module gathers what one model call may see about a project (plan 41
§5.3), makes that call and rebuilds its answer. Everything that comes
from the repository is **untrusted input** and is read from the git
object store at ``HEAD`` only, never from the working tree (D4):
:func:`read_blob` is the one reader. It resolves an exact path with ``git ls-tree -z -l``, refuses anything but a regular
blob (a symbolic link, mode ``120000``, or a submodule, ``160000``, is
skipped), streams at most the cap with ``git cat-file blob``, and returns
``None`` for a binary or undecodable blob. A symbolic link, a FIFO, a
file swapped in mid-read or an untracked local file therefore cannot be
reached, and the hardened git environment
(:func:`~whygraph.services.git.credentials.git_env`) ignores global and
system git config.

Denied names (``.env*``, ``whygraph.toml``, ``.git*``, ``.whygraph``,
``.codegraph`` and key or credential files) are refused before any git
call. Every text passes :func:`redact_purpose_text`. Manifests are parsed
and reduced to a few named keys, never dumped. History is counts, dates
and directory shares only: no commit subjects, PR titles, names or
emails.

Public API
----------
* :class:`PurposeInputs` - what :func:`gather_inputs` returns.
* :func:`gather_inputs` - every input, within the 24 KB total.
* :func:`read_blob` - the one reader of repository content.
* :func:`redact_purpose_text` - the purpose-specific redactor.
* :func:`generate` - the one model call (no retry), metered before parsing.
* :func:`rebuild_body` - validate an answer and rebuild the four lines (D9).
* :class:`PurposeResult`, :class:`PurposeInvalid` - its result and refusal.

The model sees every repository string inside a per-call random
``<repo_data_{16 hex}>`` fence (D12); its answer is never stored as
written, only the four values :func:`rebuild_body` extracts and caps.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import re
import secrets
import time
import tomllib
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.resources import files

from sqlalchemy import case, func
from sqlmodel import Session, col, select

from whygraph.chat.areas import configured_areas, overview_block, suggest_areas
from whygraph.core.config import Config
from whygraph.core.usage import record_usage
from whygraph.db.models import Commit, CommitFileChange
from whygraph.services.git import Repository, redact_tokens
from whygraph.services.git.commands import TreeEntry
from whygraph.services.llm import CompletionRequest, LlmClientFactory

from .exceptions import AnalyzeError

KB = 1024

README_CANDIDATES = ("README.md", "README.rst", "README.txt", "README")
"""Root README names, in order of preference; the first readable one is used."""

README_MAX_BYTES = 12 * KB
"""The most README bytes read."""

DOC_PATHS = ("docs/index.md", "docs/README.md", "CONTRIBUTING.md")
"""Other prose read, each up to :data:`DOC_MAX_BYTES`."""

DOC_MAX_BYTES = 4 * KB
"""The most bytes read of each of :data:`DOC_PATHS`."""

MANIFEST_READ_MAX_BYTES = 256 * KB
"""The most bytes of a manifest read before parsing (a longer one fails to parse)."""

MANIFEST_OUT_MAX_BYTES = 4 * KB
"""The most bytes of a manifest's reduced text."""

NAME_ONLY_MANIFESTS = ("pom.xml", "Gemfile", "build.gradle")
"""Manifests that contribute their file name only; their content is never read."""

LAYOUT_MAX = 60
"""The most top-level names in the layout."""

TOTAL_MAX_BYTES = 24 * KB
"""The total of every gathered input; later sources are cut first."""

HISTORY_TOP_DIRS = 5
"""Directories listed in the history's share of file changes."""

MAX_SOURCES = 20
"""The most paths :attr:`PurposeInputs.sources` lists."""

_READABLE_MODES = frozenset({"100644", "100755"})
"""Regular blob modes; ``120000`` (a link) and ``160000`` (a submodule) are not."""

_SAFE_REL = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")

_DENIED_EXACT = frozenset(
    {
        "whygraph.toml",
        ".whygraph",
        ".codegraph",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".pgpass",
        ".htpasswd",
        ".dockercfg",
    }
)
_DENIED_PREFIXES = (
    ".env",
    ".git",
    "credential",
    "secret",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
)
_DENIED_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".gpg",
    ".asc",
    ".ppk",
    ".kdbx",
)

_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*-----.*?(?:-----END [A-Z0-9 ]*-----|\Z)", re.DOTALL
)
_FENCE_TAG = re.compile(r"<\s*/?\s*repo_data[^>]*>", re.IGNORECASE)
_SECRET_LINE = re.compile(r"(?i)(password|secret|token|api[_-]?key|bearer)\s*[:=]")
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@")
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "AKIA***"),
    (re.compile(r"\bxox[a-z]-[A-Za-z0-9-]{10,}"), "xox***"),
    (re.compile(r"\b[rs]k_live_[A-Za-z0-9]{8,}"), "sk_live_***"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"), "glpat-***"),
    (re.compile(r"\bnpm_[A-Za-z0-9]{20,}"), "npm_***"),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}"), "hf_***"),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        "[redacted token]",
    ),
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PEP508_NAME = re.compile(r"[A-Za-z0-9._-]+")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class PurposeInputs:
    """Everything one purpose generation may see, already redacted and cut.

    Attributes
    ----------
    files : list of tuple of (str, str)
        ``(repo-relative path, redacted text)``: the README, then the docs,
        then the manifests. A manifest that is name-only, or failed to
        parse, has ``""`` as its text.
    layout : list of str
        Top-level names of ``HEAD``'s tree, in git's order (at most
        :data:`LAYOUT_MAX`).
    areas : list of str
        Configured area names, else suggested ones.
    history : dict
        ``{"commits", "first_commit", "last_commit", "top_dirs"}`` over
        default-branch commits; ``{}`` when cut by the total.
    input_hash : str
        sha256 hex of the file texts (in order) and the sorted top-level
        directory names (D5); history, areas and counts are not in it.
    input_bytes : int
        UTF-8 bytes of everything above after the cut (texts, names plus
        one separator each, history as JSON); at most
        :data:`TOTAL_MAX_BYTES`.
    sources : list of str
        The paths in :attr:`files` (at most :data:`MAX_SOURCES`).
    head : str or None
        The commit read, or ``None`` for a repository without one.
    """

    files: list[tuple[str, str]]
    layout: list[str]
    areas: list[str]
    history: dict
    input_hash: str
    input_bytes: int
    sources: list[str] = field(default_factory=list)
    head: str | None = None


def redact_purpose_text(text: str) -> str:
    """Scrub secrets and fence look-alikes from a repository text.

    In order: PEM blocks are replaced (an unterminated one up to the end);
    anything shaped like a ``<repo_data...>`` tag is removed (repeatedly,
    so removal cannot assemble a new one); lines matching
    ``(?i)(password|secret|token|api[_-]?key|bearer)\\s*[:=]`` are dropped;
    URL userinfo, AWS access key ids, Slack, Stripe live, GitLab, npm and
    Hugging Face tokens and JWT-shaped strings are masked; then
    :func:`~whygraph.services.git.credentials.redact_tokens` masks GitHub,
    connection and provider keys.

    Parameters
    ----------
    text : str
        Text read from the repository (or reduced from a manifest).

    Returns
    -------
    str
        The scrubbed text.
    """
    text = _PEM_BLOCK.sub("[redacted key]", text)
    while True:
        stripped = _FENCE_TAG.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = "".join(
        line for line in text.splitlines(keepends=True) if not _SECRET_LINE.search(line)
    )
    text = _URL_USERINFO.sub(r"\1***@", text)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return redact_tokens(text)


def _denied(rel: str) -> bool:
    """Whether any component of ``rel`` is a denied name (secrets, config, git)."""
    for part in rel.split("/"):
        name = part.lower()
        if (
            name in _DENIED_EXACT
            or name.startswith(_DENIED_PREFIXES)
            or name.endswith(_DENIED_SUFFIXES)
        ):
            return True
    return False


def _lookup(git: Repository, rel: str, ref: str) -> TreeEntry | None:
    """The regular blob at exactly ``rel`` in ``ref``, or ``None``.

    Denied and malformed paths are refused before any git call.
    """
    if (
        not _SAFE_REL.fullmatch(rel)
        or any(part in {".", ".."} for part in rel.split("/"))
        or _denied(rel)
    ):
        return None
    for entry in git.tree_entries(ref, rel):
        if entry.name == rel and entry.type == "blob" and entry.mode in _READABLE_MODES:
            return entry
    return None


def _decode(data: bytes, truncated: bool) -> str | None:
    """UTF-8 text of ``data``; a cut partial sequence is dropped, NUL or bad bytes give ``None``."""
    if b"\0" in data:
        return None
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(data, final=not truncated)
    except UnicodeDecodeError:
        return None
    return text.removeprefix("﻿")


def _read_entry(git: Repository, entry: TreeEntry, max_bytes: int) -> str | None:
    """Read at most ``max_bytes`` of ``entry``'s blob as text (not redacted)."""
    data, truncated = git.blob_prefix(entry.sha, max_bytes)
    if entry.size is not None and entry.size > max_bytes:
        truncated = True
    return _decode(data, truncated)


def read_blob(
    git: Repository, rel: str, max_bytes: int, *, ref: str = "HEAD"
) -> str | None:
    """Read one repository file from the git object store, redacted.

    The one reader of repository content (D4): ``git ls-tree -z -l <ref>
    -- <rel>`` (literal path, exact-case match), a regular blob only (modes
    ``120000`` and ``160000`` are skipped), then ``git cat-file blob`` up to
    ``max_bytes``. A trailing partial UTF-8 sequence left by the cut is
    dropped. The working tree is never opened.

    Parameters
    ----------
    git : Repository
        The project's repository.
    rel : str
        A root-relative path (``[A-Za-z0-9._-]`` components only).
    max_bytes : int
        The most bytes read.
    ref : str, optional
        The commit to read. Default ``"HEAD"``; :func:`gather_inputs` passes
        the resolved SHA so every read sees one commit.

    Returns
    -------
    str or None
        The text after :func:`redact_purpose_text`; ``None`` when the path
        is denied, malformed, absent, not a regular blob, holds a NUL byte
        or is not UTF-8.

    Raises
    ------
    whygraph.services.git.GitError
        If ``git`` fails.
    """
    entry = _lookup(git, rel, ref)
    if entry is None:
        return None
    text = _read_entry(git, entry, max_bytes)
    return None if text is None else redact_purpose_text(text)


# --- manifests ---------------------------------------------------------------


def _clean(value: object) -> str:
    """A one-line string (control characters become spaces); ``""`` for a non-string."""
    if not isinstance(value, str):
        return ""
    return " ".join(_CONTROL.sub(" ", value).split())


def _keys(value: object) -> list[str]:
    return [_clean(k) for k in value if _clean(k)] if isinstance(value, dict) else []


def _table(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _pep508_names(value: object) -> list[str]:
    """Distribution names of PEP 508 requirement strings (no versions, extras or markers)."""
    names: list[str] = []
    for item in value if isinstance(value, list) else []:
        match = _PEP508_NAME.match(item.strip()) if isinstance(item, str) else None
        if match and match.group() not in names:
            names.append(match.group())
    return names


def _lines(*pairs: tuple[str, str | list[str]]) -> str:
    """``label: value`` lines, skipping empty values; a list is comma-joined."""
    out = []
    for label, value in pairs:
        text = ", ".join(v for v in value if v) if isinstance(value, list) else value
        if text:
            out.append(f"{label}: {text}")
    return "\n".join(out)


def _reduce_package_json(text: str) -> str:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return _lines(
        ("name", _clean(data.get("name"))),
        ("description", _clean(data.get("description"))),
        ("scripts", _keys(data.get("scripts"))),
        ("dependencies", _keys(data.get("dependencies"))),
        ("devDependencies", _keys(data.get("devDependencies"))),
        ("peerDependencies", _keys(data.get("peerDependencies"))),
    )


def _reduce_pyproject(text: str) -> str:
    data = tomllib.loads(text)
    project = _table(data.get("project"))
    optional: list[str] = []
    for extra in _table(project.get("optional-dependencies")).values():
        optional.extend(n for n in _pep508_names(extra) if n not in optional)
    poetry = _table(_table(data.get("tool")).get("poetry"))
    poetry_deps = _keys(poetry.get("dependencies")) + _keys(
        poetry.get("dev-dependencies")
    )
    for group in _table(poetry.get("group")).values():
        poetry_deps += _keys(_table(group).get("dependencies"))
    return _lines(
        ("name", _clean(project.get("name"))),
        ("description", _clean(project.get("description"))),
        ("dependencies", _pep508_names(project.get("dependencies"))),
        ("optional-dependencies", optional),
        ("poetry dependencies", list(dict.fromkeys(poetry_deps))),
    )


def _reduce_cargo(text: str) -> str:
    data = tomllib.loads(text)
    package = _table(data.get("package"))
    return _lines(
        ("name", _clean(package.get("name"))),
        ("description", _clean(package.get("description"))),
        ("dependencies", _keys(data.get("dependencies"))),
        ("dev-dependencies", _keys(data.get("dev-dependencies"))),
    )


def _reduce_composer(text: str) -> str:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return _lines(
        ("name", _clean(data.get("name"))),
        ("description", _clean(data.get("description"))),
        ("require", _keys(data.get("require"))),
    )


def _reduce_go_mod(text: str) -> str:
    module = ""
    requires: list[str] = []
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if in_block:
            if line == ")":
                in_block = False
            elif line.split()[0] not in requires:
                requires.append(_clean(line.split()[0]))
            continue
        words = line.split()
        if words[0] == "module" and len(words) > 1 and not module:
            module = _clean(words[1])
        elif words[0] == "require":
            if words[1:] == ["("]:
                in_block = True
            elif len(words) > 1 and words[1] not in requires:
                requires.append(_clean(words[1]))
    return _lines(("module", module), ("require", requires))


MANIFEST_REDUCERS: dict[str, Callable[[str], str]] = {
    "package.json": _reduce_package_json,
    "pyproject.toml": _reduce_pyproject,
    "Cargo.toml": _reduce_cargo,
    "composer.json": _reduce_composer,
    "go.mod": _reduce_go_mod,
}
"""Manifests reduced to named keys, in the order they are gathered."""


def _clip_bytes(text: str, max_bytes: int) -> str:
    """``text`` cut to at most ``max_bytes`` UTF-8 bytes, never mid-character."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", "ignore")


def _manifest(git: Repository, rel: str, ref: str) -> str | None:
    """The reduced, capped and redacted text of manifest ``rel``; ``None`` if absent.

    A name-only manifest, or one that is undecodable or fails to parse
    (``RecursionError`` included), contributes ``""``: its name only.
    """
    entry = _lookup(git, rel, ref)
    if entry is None:
        return None
    reducer = MANIFEST_REDUCERS.get(rel)
    if reducer is None:
        return ""
    text = _read_entry(git, entry, MANIFEST_READ_MAX_BYTES)
    if text is None:
        return ""
    try:
        reduced = reducer(text)
    except Exception:  # noqa: BLE001 - RecursionError and any parse error: name only
        return ""
    return redact_purpose_text(_clip_bytes(reduced, MANIFEST_OUT_MAX_BYTES))


# --- layout, areas, history -------------------------------------------------


def _name(raw: str) -> str:
    """A repository-derived name, one line and redacted (may become ``""``)."""
    return redact_purpose_text(_CONTROL.sub("?", raw)).strip()


def _areas(session: Session) -> list[str]:
    if configured_areas():
        names = [item["name"] for item in overview_block()["items"]]
    else:
        names = [entry["name"] for entry in suggest_areas(session)]
    return [n for n in (_name(str(name)) for name in names) if n]


def _date(value: str | None) -> str | None:
    head = (value or "")[:10]
    return head if _DATE.fullmatch(head) else None


def _history(session: Session) -> dict:
    """Counts over default-branch commits: no subjects, titles, names or emails."""
    on_default = col(Commit.on_default_branch) == 1
    count, first, last = session.exec(
        select(
            func.count(), func.min(Commit.authored_at), func.max(Commit.authored_at)
        ).where(on_default)
    ).one()
    path = col(CommitFileChange.path)
    slash = func.instr(path, "/")
    segment = case((slash > 1, func.substr(path, 1, slash - 1)), else_=None)
    rows = session.exec(
        select(segment, func.count())
        .join(Commit, col(Commit.sha) == col(CommitFileChange.commit_sha))
        .where(on_default)
        .group_by(segment)
    ).all()
    total = sum(n for _, n in rows)
    dirs = sorted(((d, n) for d, n in rows if d), key=lambda item: (-item[1], item[0]))
    top = []
    for directory, n in dirs:
        name = _name(directory)
        if name:
            top.append({"dir": name, "share": round(100 * n / total)})
        if len(top) == HISTORY_TOP_DIRS:
            break
    return {
        "commits": int(count or 0),
        "first_commit": _date(first),
        "last_commit": _date(last),
        "top_dirs": top,
    }


# --- the total and the hash -------------------------------------------------

_CUT_PRIORITY = ("readme", "manifest", "docs")
"""File kinds in the order they keep their bytes (the README is cut last)."""


def _fit_budget(
    files: list[tuple[str, str]],
    kinds: dict[str, str],
    layout: list[str],
    areas: list[str],
    history: dict,
    budget: int,
) -> tuple[list[tuple[str, str]], list[str], list[str], dict, int]:
    """Cut the inputs to ``budget`` bytes, later sources first.

    Bytes are spent in the order README, manifests, docs, layout, areas,
    history; the source that crosses the budget is cut (a text at a
    character boundary, a list item by item) and every later one is
    dropped. Returns the kept files (in their original order), layout,
    areas, history and the bytes used.
    """
    remaining = budget
    cut = False
    kept: dict[str, str] = {}
    for kind in _CUT_PRIORITY:
        for path, text in files:
            if kinds.get(path) != kind:
                continue
            size = len(text.encode("utf-8"))
            if size <= remaining:
                kept[path] = text
                remaining -= size
                continue
            cut = True
            if remaining > 0:
                kept[path] = _clip_bytes(text, remaining)
                remaining -= len(kept[path].encode("utf-8"))

    def take(items: list[str]) -> list[str]:
        nonlocal remaining, cut
        out: list[str] = []
        for item in items:
            cost = len(item.encode("utf-8")) + 1
            if cut or cost > remaining:
                cut = True
                break
            out.append(item)
            remaining -= cost
        return out

    kept_layout = take(layout)
    kept_areas = take(areas)
    kept_history: dict = {}
    cost = len(json.dumps(history))
    if not cut and cost <= remaining:
        kept_history = history
        remaining -= cost
    return (
        [(path, kept[path]) for path, _ in files if path in kept],
        kept_layout,
        kept_areas,
        kept_history,
        budget - remaining,
    )


def _input_hash(files: list[tuple[str, str]], top_dirs: list[str]) -> str:
    """sha256 of the file paths and texts in order, then the sorted top-level directories."""
    digest = hashlib.sha256(b"whygraph-project-purpose-inputs-v1\0")
    for path, text in files:
        digest.update(path.encode("utf-8") + b"\0" + text.encode("utf-8") + b"\0")
    digest.update(b"\x1e")
    for name in sorted(top_dirs):
        digest.update(name.encode("utf-8") + b"\0")
    return digest.hexdigest()


def gather_inputs(session: Session, git: Repository) -> PurposeInputs:
    """Gather every input of a purpose generation (plan 41 §5.3).

    ``HEAD`` is resolved once and every read uses that commit. The README
    is the first readable of :data:`README_CANDIDATES` (at most 12 KB);
    then each of :data:`DOC_PATHS` (4 KB each); then the manifests,
    reduced (:data:`MANIFEST_REDUCERS`) or name-only
    (:data:`NAME_ONLY_MANIFESTS`); the layout is the top-level names of
    ``HEAD``'s tree (at most :data:`LAYOUT_MAX`); the areas are the
    configured ones, else :func:`~whygraph.chat.areas.suggest_areas`; the
    history is counts over default-branch commits. The whole is cut to
    :data:`TOTAL_MAX_BYTES`, history first and the README last. The
    working tree is never read.

    Parameters
    ----------
    session : sqlmodel.Session
        An open session on the project's WhyGraph database.
    git : Repository
        The project's repository.

    Returns
    -------
    PurposeInputs
        Redacted, cut inputs and their hash. A repository without a commit
        gives no files and no layout.

    Raises
    ------
    whygraph.services.git.GitError
        If ``git`` fails.
    """
    head = git.head_commit()
    files: list[tuple[str, str]] = []
    kinds: dict[str, str] = {}
    layout: list[str] = []
    top_dirs: list[str] = []
    if head is not None:
        for rel in README_CANDIDATES:
            text = read_blob(git, rel, README_MAX_BYTES, ref=head)
            if text is not None:
                files.append((rel, text))
                kinds[rel] = "readme"
                break
        for rel in DOC_PATHS:
            text = read_blob(git, rel, DOC_MAX_BYTES, ref=head)
            if text is not None:
                files.append((rel, text))
                kinds[rel] = "docs"
        for rel in (*MANIFEST_REDUCERS, *NAME_ONLY_MANIFESTS):
            text = _manifest(git, rel, head)
            if text is not None:
                files.append((rel, text))
                kinds[rel] = "manifest"
        top = git.tree_entries(head)
        layout = [n for n in (_name(e.name) for e in top[:LAYOUT_MAX]) if n]
        top_dirs = [n for n in (_name(e.name) for e in top if e.type == "tree") if n]
    files, layout, areas, history, used = _fit_budget(
        files, kinds, layout, _areas(session), _history(session), TOTAL_MAX_BYTES
    )
    return PurposeInputs(
        files=files,
        layout=layout,
        areas=areas,
        history=history,
        input_hash=_input_hash(files, top_dirs),
        input_bytes=used,
        sources=[path for path, _ in files][:MAX_SOURCES],
        head=head,
    )


# --- generation (D9, D11, D12) ----------------------------------------------

PURPOSE_SUBJECT = "project-purpose"
"""The usage ledger's ``subject`` for a purpose generation (task ``analyze``)."""

PURPOSE_LABELS = ("What it is", "Who it is for", "Main parts", "Stack")
"""The four labels of a purpose body, in order (case-sensitive)."""

PART_CAPS = {"What it is": 200, "Who it is for": 150, "Main parts": 250, "Stack": 200}
"""The most characters of each part's value."""

STACK_MAX_ITEMS = 12
"""The most comma-separated items in ``Stack``."""

STACK_ITEM_MAX = 40
"""The most characters of one ``Stack`` item."""

BODY_MAX_CHARS = 600
"""The most characters of the rebuilt four-line body."""

_MAIN_PARTS_FLOOR = 80
"""``Main parts`` is shortened down to this before ``Stack`` items are dropped."""

_TRUNCATED = frozenset({"length", "max_tokens"})
"""``finish_reason`` values of an answer cut at the token limit (OpenAI, Anthropic)."""

_FORBIDDEN = ("://", "www.", "](", "`", "|", "$(", "&&", ";", ">")
"""Substrings that reject an answer (checked on the lower-cased normalized text)."""

_INVISIBLE = frozenset(
    chr(code)
    for code in (
        0x061C,
        0x180E,
        *range(0x200B, 0x2010),
        *range(0x202A, 0x202F),
        0x2060,
        *range(0x2066, 0x206A),
        0xFEFF,
    )
)
"""Zero-width and bidirectional control characters; any of them rejects (AC 4)."""

_STRIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs"})
_LABEL_LINE = re.compile(r"(What it is|Who it is for|Main parts|Stack)[*_]*[ ]*:(.*)")
_BULLET = re.compile(r" *[-*] ")


class PurposeInvalid(AnalyzeError):
    """The model's answer cannot be used as a purpose body (D9).

    Raised after the call was recorded in the usage ledger; the caller
    marks the run ``failed`` and never retries (no second spend).

    Attributes
    ----------
    error_code : str
        Always ``"purpose_invalid"``.
    """

    error_code = "purpose_invalid"


@dataclass(frozen=True)
class PurposeResult:
    """One accepted purpose generation.

    Attributes
    ----------
    body : str
        The rebuilt four labelled lines (:func:`rebuild_body`), at most
        :data:`BODY_MAX_CHARS` characters; never the raw model text.
    model : str
        The model the provider reports serving (else the one requested).
    provider : str
        The adapter's provider tag.
    tokens_in : int or None
        Prompt tokens, when reported.
    tokens_out : int or None
        Completion tokens, when reported.
    """

    body: str
    model: str
    provider: str
    tokens_in: int | None
    tokens_out: int | None


def _system_prompt() -> str:
    """The packaged system prompt (no ladder: one file for every provider)."""
    path = files("whygraph.analyze") / "prompts" / "project_purpose" / "default"
    return (path / "system.md").read_text(encoding="utf-8").strip()


def _unfence(text: object, tag: str) -> str:
    """``text`` without any ``<repo_data...>`` tag or the call's ``tag`` name."""
    value = str(text)
    while True:
        stripped = _FENCE_TAG.sub("", value).replace(tag, "")
        if stripped == value:
            return value
        value = stripped


def _user_message(inputs: PurposeInputs, tag: str) -> str:
    """The user message: one fence, ``<tag>``, around every repository string (D12)."""
    sections: list[str] = []
    for path, text in inputs.files:
        body = _unfence(text, tag).strip() or "(listed by name only)"
        sections.append(f"### File: {_unfence(path, tag)}\n{body}")
    for title, names in (("Top-level names", inputs.layout), ("Areas", inputs.areas)):
        kept = [n for n in (_unfence(name, tag).strip() for name in names) if n]
        if kept:
            sections.append(f"### {title}\n" + "\n".join(kept))
    history = inputs.history
    if history:
        lines = [
            f"Commits on the default branch: {_unfence(history.get('commits'), tag)}",
            f"First commit: {_unfence(history.get('first_commit') or 'unknown', tag)}",
            f"Last commit: {_unfence(history.get('last_commit') or 'unknown', tag)}",
        ]
        for entry in history.get("top_dirs") or []:
            name = _unfence(entry.get("dir", ""), tag).strip()
            if name:
                share = _unfence(entry.get("share", ""), tag)
                lines.append(f"Share of file changes in {name}: {share}%")
        sections.append("### History\n" + "\n".join(lines))
    data = "\n\n".join(sections) or "(no data)"
    return (
        f"Write the project card from the repository data in the {tag} block.\n\n"
        f"<{tag}>\n{data}\n</{tag}>\n\n"
        "Answer with the four labelled lines only."
    )


def generate(
    inputs: PurposeInputs,
    *,
    config: Config,
    factory: LlmClientFactory | None = None,
) -> PurposeResult:
    """Run the one model call of a purpose generation and rebuild its answer.

    The provider and model come from ``config.model_for("analyze")`` (D2)
    through :class:`~whygraph.services.llm.LlmClientFactory`, as
    :meth:`~whygraph.analyze.LlmDescriptor.from_config` does, and the
    timeout from ``config.timeout_for("analyze")``. The request never
    retries (``max_retries=0``, D11) and leaves ``temperature`` and
    ``max_tokens`` at the provider defaults, like the descriptor. Every
    repository string goes inside a per-call ``<repo_data_{16 hex}>``
    fence, with any occurrence of that tag stripped from the inputs; the
    system message holds none (D12). The call is recorded with
    :func:`~whygraph.core.usage.record_usage` (task ``analyze``, subject
    :data:`PURPOSE_SUBJECT`) as soon as the provider returns, before the
    answer is checked.

    Parameters
    ----------
    inputs : PurposeInputs
        What :func:`gather_inputs` returned.
    config : Config
        The project's configuration.
    factory : LlmClientFactory, optional
        Resolves the provider into a client. Defaults to a fresh
        :class:`~whygraph.services.llm.LlmClientFactory`; tests inject one
        with a stub adapter.

    Returns
    -------
    PurposeResult
        The rebuilt body and the call's provenance.

    Raises
    ------
    PurposeInvalid
        If the answer was cut at the token limit or fails
        :func:`rebuild_body`. Not retried; the usage is already recorded.
    whygraph.services.llm.LlmError
        If the provider is unknown, its preflight fails or the call fails
        (nothing is recorded then).
    """
    factory = factory if factory is not None else LlmClientFactory()
    provider, model = config.model_for("analyze")
    client = factory.make(provider, model=model)
    client.preflight()
    tag = f"repo_data_{secrets.token_hex(8)}"
    request = CompletionRequest.of(
        _user_message(inputs, tag),
        system=_system_prompt(),
        timeout_sec=config.timeout_for("analyze"),
        max_retries=0,
    )
    started = time.monotonic()
    response = client.complete(request)
    record_usage(
        "analyze",
        response,
        provider=client.provider,
        model_requested=client.model,
        subject=PURPOSE_SUBJECT,
        started=started,
    )
    if response.finish_reason in _TRUNCATED:
        raise PurposeInvalid("the answer was cut at the token limit")
    return PurposeResult(
        body=rebuild_body(response.text),
        model=response.model or client.model,
        provider=client.provider,
        tokens_in=response.input_tokens,
        tokens_out=response.output_tokens,
    )


def _normalize(text: str) -> str:
    """NFKC, then reject invisible characters and drop Cc/Cf/Co/Cs except ``\\n``."""
    text = unicodedata.normalize("NFKC", text)
    if any(ch in _INVISIBLE for ch in text):
        raise PurposeInvalid("the answer contains a zero-width or bidi character")
    return "".join(
        ch
        for ch in text
        if ch == "\n" or unicodedata.category(ch) not in _STRIPPED_CATEGORIES
    )


def _tidy(value: str) -> str:
    """One line: whitespace collapsed, markdown ``*`` trimmed at both ends."""
    return " ".join(value.split()).strip("* ")


def _clip(text: str, limit: int) -> str:
    """``text`` within ``limit`` characters, cut at a word boundary when one is near."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.:-")


def _extract(text: str) -> dict[str, str]:
    """The four labelled values of a normalized answer, in order.

    Leading ``*``, ``#``, ``-`` and spaces are stripped per line, so
    markdown-decorated labels count. A line that is not a label continues
    the current value (a bullet joins with ``", "``, prose with ``" "``);
    a blank line ends it, and lines before the first label are ignored.
    """
    parts: dict[str, str] = {}
    current: str | None = None
    for raw in text.split("\n"):
        line = raw.lstrip("*#- ").rstrip()
        if not line:
            current = None
            continue
        match = _LABEL_LINE.fullmatch(line)
        if match is not None:
            label = match.group(1)
            if len(parts) == len(PURPOSE_LABELS) or label != PURPOSE_LABELS[len(parts)]:
                raise PurposeInvalid(f"label {label!r} is repeated or out of order")
            parts[label] = _tidy(match.group(2))
            current = label
        elif current is not None:
            piece = _tidy(line)
            if piece:
                sep = ", " if _BULLET.match(raw) else " "
                parts[current] = (
                    f"{parts[current]}{sep}{piece}" if parts[current] else piece
                )
    missing = [label for label in PURPOSE_LABELS if label not in parts]
    if missing:
        raise PurposeInvalid(f"missing label {missing[0]!r}")
    return parts


def _stack_items(value: str) -> list[str]:
    """``Stack`` as at most :data:`STACK_MAX_ITEMS` items of :data:`STACK_ITEM_MAX`."""
    items = [_clip(_tidy(item), STACK_ITEM_MAX) for item in value.split(",")]
    items = [item for item in items if item][:STACK_MAX_ITEMS]
    while len(items) > 1 and len(", ".join(items)) > PART_CAPS["Stack"]:
        items.pop()
    return items or ["unknown"]


def _render(parts: dict[str, str], stack: list[str]) -> str:
    values = {**parts, "Stack": ", ".join(stack)}
    return "\n".join(f"{label}: {values[label]}" for label in PURPOSE_LABELS)


def rebuild_body(text: str) -> str:
    """Validate a model answer and rebuild it as four labelled lines (D9).

    In order: NFKC-normalize; reject any zero-width or bidirectional
    control character (AC 4); strip the remaining characters of category
    Cc, Cf, Co and Cs except ``\\n``; reject when the text contains
    ``://``, ``www.`` (any case), ``](``, a backtick, ``|``, ``$(``,
    ``&&``, ``;`` or ``>``; strip leading ``*``, ``#``, ``-`` and spaces
    per line; extract the values of ``What it is:``, ``Who it is for:``,
    ``Main parts:`` and ``Stack:`` (case-sensitive, each once, in order;
    a missing one rejects, an empty one becomes ``unknown``); run
    :func:`~whygraph.services.git.credentials.redact_tokens` over each
    value; cap What at 200, Who at 150, Main parts at 250 and Stack at 200
    characters, ``Stack`` being at most 12 comma-separated items of at
    most 40; then shorten Main parts (down to 80) and drop trailing Stack
    items until the body is at most :data:`BODY_MAX_CHARS`. Words such as
    "httpx" or "HTTP API" pass.

    Parameters
    ----------
    text : str
        The model's raw answer.

    Returns
    -------
    str
        ``"What it is: ...\\nWho it is for: ...\\nMain parts: ...\\nStack: ..."``,
        built from the extracted values only, never the raw text.

    Raises
    ------
    PurposeInvalid
        If the answer is rejected.
    """
    text = _normalize(text)
    lowered = text.lower()
    for needle in _FORBIDDEN:
        if needle in lowered:
            raise PurposeInvalid(f"the answer contains {needle!r}")
    parts = _extract(text)
    for label in PURPOSE_LABELS:
        value = redact_tokens(parts[label])
        if label != "Stack":
            value = _clip(value, PART_CAPS[label])
        parts[label] = value or "unknown"
    stack = _stack_items(parts["Stack"])
    body = _render(parts, stack)
    overflow = len(body) - BODY_MAX_CHARS
    if overflow > 0:
        main = parts["Main parts"]
        limit = max(_MAIN_PARTS_FLOOR, len(main) - overflow)
        parts["Main parts"] = _clip(main, limit) or "unknown"
        body = _render(parts, stack)
    while len(body) > BODY_MAX_CHARS and len(stack) > 1:
        stack.pop()
        body = _render(parts, stack)
    return body
