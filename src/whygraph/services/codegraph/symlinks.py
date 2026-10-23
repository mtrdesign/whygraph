"""Find the symbolic links that would let CodeGraph index files outside a repository.

CodeGraph follows a symbolic link out of the project folder on purpose
(upstream #935: its indexer reads files with ``allowSymlinkEscape``), at
any depth, relative or absolute, tracked or not, and a committed
``codegraph.json`` ``include`` walks into a linked folder. A repository is
untrusted input, so before every CodeGraph write WhyGraph lists what
CodeGraph would read and refuses to run it while one of those paths is a
link that resolves outside the root (:func:`escaping_symlinks`). It
refuses rather than excluding the link: there is no escape hatch.

The candidates mirror CodeGraph's own file enumeration (``getGitVisibleFiles``
and ``collectIncludedFiles`` in its ``extraction/index.js``):

* every path ``git ls-files -s --recurse-submodules`` lists that is a link on
  disk (the mode ``120000`` entries, and a tracked file replaced by a link);
* every untracked, non-ignored path (``git ls-files -o --exclude-standard``)
  that is a link on disk - a tracked folder replaced by a link shows up here;
* the same two listings inside every embedded repository CodeGraph descends
  into: an untracked or gitlinked folder with its own ``.git``, and, when
  ``codegraph.json`` sets ``includeIgnored``, one found inside a gitignored
  folder;
* when ``codegraph.json`` sets ``include``: a walk of each pattern's literal
  leading folder, following in-root folder links as CodeGraph does, pruning
  ``.git``, ``.codegraph*`` and CodeGraph's default-ignored folders.

It never walks the whole tree unconditionally: ``.venv/bin/python`` is a
link out of the repository in most Python checkouts, and CodeGraph never
indexes it. Where it cannot mirror CodeGraph exactly it errs towards
refusing (an ``include`` walk judges every link, not only the files the
patterns match). A folder that is not a git work tree, which CodeGraph
would walk in full, is refused outright.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from whygraph.core.safe_paths import resolves_inside
from whygraph.services.git.credentials import pass_through_env

from .exceptions import CodeGraphBootstrapError

GIT_TIMEOUT_SEC = 60
"""Seconds before one ``git ls-files`` of the guard is killed (the refresh then fails)."""

CONFIG_FILE = "codegraph.json"
"""CodeGraph's project configuration, read from the repository root."""

_DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        "bower_components",
        "jspm_packages",
        "web_modules",
        ".yarn",
        ".pnpm-store",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".turbo",
        ".vite",
        ".parcel-cache",
        ".angular",
        ".docusaurus",
        "storybook-static",
        ".vinxi",
        ".nitro",
        "out-tsc",
        ".vercel",
        ".netlify",
        ".wrangler",
        "dist",
        "build",
        "out",
        ".output",
        "coverage",
        ".nyc_output",
        "__pycache__",
        "__pypackages__",
        ".venv",
        "venv",
        ".pixi",
        ".pdm-build",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".hypothesis",
        ".ipynb_checkpoints",
        ".eggs",
        "target",
        ".gradle",
        "obj",
        "vendor",
        ".build",
        "Pods",
        "Carthage",
        "DerivedData",
        ".swiftpm",
        ".dart_tool",
        ".pub-cache",
        ".cxx",
        ".externalNativeBuild",
        "vcpkg_installed",
        ".bloop",
        ".metals",
        "lua_modules",
        ".luarocks",
        "__history",
        "__recovery",
        ".cache",
    }
)
"""CodeGraph 1.4.1's ``DEFAULT_IGNORE_DIRS``: folders it never walks into by name."""

_DEFAULT_IGNORE_GLOBS = ("*.egg-info", "cmake-build-*", "bazel-*")
"""The glob folder patterns of CodeGraph's ``DEFAULT_IGNORE_PATTERNS``."""

_ANDROID_RES_TYPES = (
    "anim",
    "animator",
    "color",
    "drawable",
    "font",
    "layout",
    "menu",
    "mipmap",
    "navigation",
    "transition",
    "values",
    "xml",
)
"""Android ``res/<type>*/`` folders CodeGraph ignores at any depth."""

_GLOB_META = re.compile(r"[*?\[\]{}!]")
"""Characters that end the literal prefix of an ``include`` pattern (CodeGraph's ``GLOB_META``)."""

_NESTED_REPO_DEPTH = 4
"""How deep CodeGraph looks below a gitignored folder for an embedded repository."""


@dataclass(frozen=True)
class _ProjectConfig:
    """The two ``codegraph.json`` keys that widen what CodeGraph reads."""

    include: tuple[str, ...] = ()
    include_ignored: tuple[str, ...] = ()


def escaping_symlinks(root: Path) -> tuple[str, ...]:
    """List the links CodeGraph would read through that lead out of ``root``.

    Parameters
    ----------
    root : Path
        The repository root CodeGraph indexes (its working directory).

    Returns
    -------
    tuple[str, ...]
        Root-relative POSIX paths, sorted, of every candidate (see the
        module docstring) that resolves outside ``root``; a dangling link
        is judged by where it points. Empty when indexing is safe. A link
        that stays inside the root is allowed.

    Raises
    ------
    CodeGraphBootstrapError
        When ``root`` is not a git work tree CodeGraph would list with git
        (it would walk the whole folder instead), or a ``git ls-files``
        fails or times out: nothing can be judged, so nothing is indexed.
    """
    root = Path(root)
    found: set[str] = set()

    def judge(rel: str) -> bool:
        """Record ``rel`` when it resolves outside the root; whether it stays inside."""
        if resolves_inside(root, root / rel):
            return True
        found.add(rel)
        return False

    config = _read_config(root, judge)
    _require_git_root(root)
    _scan_repo(root, root, "", bool(config.include_ignored), judge, set())
    for lead in _include_roots(config.include):
        _walk_include(root, lead, judge)
    return tuple(sorted(found))


def _read_config(root: Path, judge: Callable[[str], bool]) -> _ProjectConfig:
    """Read ``codegraph.json`` as CodeGraph does: anything malformed is no config.

    A ``codegraph.json`` that is itself a link out of the root is recorded
    (and not read).
    """
    path = root / CONFIG_FILE
    if not os.path.lexists(path) or not judge(CONFIG_FILE):
        return _ProjectConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return _ProjectConfig()
    if not isinstance(data, dict):
        return _ProjectConfig()
    return _ProjectConfig(
        include=_patterns(data.get("include")),
        include_ignored=_patterns(data.get("includeIgnored")),
    )


def _patterns(raw: object) -> tuple[str, ...]:
    """The non-empty, trimmed string entries of a pattern list (others are skipped)."""
    if not isinstance(raw, list):
        return ()
    return tuple(e.strip() for e in raw if isinstance(e, str) and e.strip())


def _git_env() -> dict[str, str]:
    """The child-env allowlist, no user or system git config, no optional locks."""
    env = pass_through_env()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run ``git <args>`` in ``cwd`` (bytes out); a missing git or a timeout raises."""
    try:
        return subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args],
            cwd=cwd,
            env=_git_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=GIT_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodeGraphBootstrapError(
            f"refusing to index: could not list the repository's files to check"
            f" for symbolic links (git {args[0]}: {exc})"
        ) from exc


def _ls_files(cwd: Path, *args: str) -> list[str]:
    """``git ls-files -z <args>`` in ``cwd`` as a list of paths (or ``-s`` lines)."""
    result = _run_git(cwd, "ls-files", "-z", *args)
    if result.returncode != 0:
        lines = os.fsdecode(result.stderr).strip().splitlines()
        detail = lines[-1] if lines else f"exit {result.returncode}"
        raise CodeGraphBootstrapError(
            "refusing to index: could not list the repository's files to check"
            f" for symbolic links ({detail})"
        )
    return [os.fsdecode(entry) for entry in result.stdout.split(b"\0") if entry]


def _require_git_root(root: Path) -> None:
    """Raise unless CodeGraph would enumerate ``root`` with git (not a folder walk)."""
    top = _run_git(root, "rev-parse", "--show-toplevel")
    if top.returncode == 0:
        toplevel = os.fsdecode(top.stdout).strip()
        if os.path.realpath(toplevel) == os.path.realpath(root):
            return
        # A subfolder of a repository: CodeGraph walks it when git ignores it.
        ignored = _run_git(root, "check-ignore", "-q", "--", os.fspath(root))
        if ignored.returncode == 1:
            return
    raise CodeGraphBootstrapError(
        "refusing to index: the project folder is not a git repository (or git"
        " ignores it), so WhyGraph cannot check the files CodeGraph would read"
        " for symbolic links"
    )


def _has_git(path: Path) -> bool:
    """Whether ``path`` holds a ``.git`` entry (an embedded repository or a worktree)."""
    return os.path.exists(path / ".git")


def _scan_repo(
    root: Path,
    repo: Path,
    prefix: str,
    include_ignored: bool,
    judge: Callable[[str], bool],
    seen: set[str],
) -> None:
    """Judge the links among ``repo``'s tracked and untracked files, then its embedded repos."""
    real = os.path.realpath(repo)
    if real in seen:
        return
    seen.add(real)
    nested: list[str] = []
    for entry in _ls_files(repo, "-s", "--recurse-submodules"):
        meta, tab, rel = entry.partition("\t")
        if not tab:
            continue
        if meta.startswith("160000"):
            # A gitlink --recurse-submodules left unexpanded: CodeGraph
            # descends into it when it has a checkout of its own.
            if _has_git(repo / rel):
                nested.append(rel.rstrip("/"))
            continue
        if os.path.islink(repo / rel):
            judge(prefix + rel)
    for rel in _ls_files(repo, "-o", "--exclude-standard"):
        if rel.endswith("/"):
            # Git lists an embedded repository as one "dir/" entry.
            if _has_git(repo / rel):
                nested.append(rel.rstrip("/"))
            continue
        if os.path.islink(repo / rel):
            judge(prefix + rel)
    if include_ignored:
        listing = _ls_files(repo, "-o", "-i", "--exclude-standard", "--directory")
        for rel in listing:
            if not rel.endswith("/") or rel in ("./", "."):
                continue
            rel = rel.rstrip("/")
            if not _default_ignored(rel):
                nested.extend(_nested_repos(repo, rel))
    for rel in nested:
        if judge(prefix + rel):
            _scan_repo(
                root, repo / rel, f"{prefix}{rel}/", include_ignored, judge, seen
            )


def _nested_repos(repo: Path, rel: str) -> list[str]:
    """Repositories at or below ``repo/rel`` (CodeGraph's ``findNestedGitRepos``, uncapped)."""
    found: list[str] = []
    queue = [(rel, 0)]
    while queue:
        current, depth = queue.pop(0)
        path = repo / current
        if _has_git(path):
            found.append(current)
            continue
        if depth >= _NESTED_REPO_DEPTH:
            continue
        try:
            entries = list(os.scandir(path))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name == ".git" or _is_codegraph_dir(entry.name):
                continue
            child = f"{current}/{entry.name}"
            if not _default_ignored(child):
                queue.append((child, depth + 1))
    return found


def _include_roots(patterns: tuple[str, ...]) -> list[str]:
    """The literal leading folder of each ``include`` pattern (CodeGraph's ``includeStaticRoots``).

    Root-relative, ``"/"``-terminated; ``[""]`` (the whole tree) when a
    pattern starts with a glob. A folder nested under another is dropped.
    """
    roots: set[str] = set()
    for pattern in patterns:
        text = pattern.lstrip("/")
        trailing = text.endswith("/")
        if trailing:
            text = text[:-1]
        segments = [s for s in text.split("/") if s]
        lead: list[str] = []
        for segment in segments:
            if _GLOB_META.search(segment):
                break
            lead.append(segment)
        wildcard = len(lead) < len(segments)
        if not wildcard and not trailing and lead:
            lead.pop()  # a literal file name: walk the folder it is in
        if not lead:
            return [""]
        roots.add("/".join(lead) + "/")
    return sorted(
        r for r in roots if not any(o != r and r.startswith(o) for o in roots)
    )


def _walk_include(root: Path, lead: str, judge: Callable[[str], bool]) -> None:
    """Walk one ``include`` root as CodeGraph does, judging every link on the way."""
    start_rel = ""
    if lead:
        start_rel = os.path.normpath(lead.rstrip("/")).replace(os.sep, "/")
        if start_rel == ".." or start_rel.startswith("../") or os.path.isabs(start_rel):
            return  # CodeGraph skips every path that leaves the root lexically
        if start_rel == ".":
            start_rel = ""
    start = root / start_rel if start_rel else root
    if not os.path.lexists(start):
        return
    if start_rel and not judge(start_rel):
        return
    seen: set[str] = set()
    stack = [(start, start_rel)]
    while stack:
        folder, folder_rel = stack.pop()
        real = os.path.realpath(folder)
        if real in seen:
            continue
        seen.add(real)
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            if entry.name == ".git" or _is_codegraph_dir(entry.name):
                continue
            rel = f"{folder_rel}/{entry.name}" if folder_rel else entry.name
            path = folder / entry.name
            if entry.is_symlink():
                if judge(rel) and path.is_dir() and not _default_ignored(rel):
                    stack.append((path, rel))
            elif entry.is_dir(follow_symlinks=False) and not _default_ignored(rel):
                stack.append((path, rel))


def _is_codegraph_dir(name: str) -> bool:
    """CodeGraph's ``isCodeGraphDataDir``: ``.codegraph`` or ``.codegraph-*``."""
    return name == ".codegraph" or name.startswith(".codegraph-")


def _default_ignored(rel: str) -> bool:
    """Whether CodeGraph's built-in defaults ignore the folder ``rel`` (or one above it)."""
    parts = [p for p in rel.split("/") if p]
    for i, name in enumerate(parts):
        if name in _DEFAULT_IGNORE_DIRS:
            return True
        if any(fnmatch.fnmatchcase(name, glob) for glob in _DEFAULT_IGNORE_GLOBS):
            return True
        if i > 0 and parts[i - 1] == "res":
            if any(name.startswith(kind) for kind in _ANDROID_RES_TYPES):
                return True
    return False


__all__ = ["CONFIG_FILE", "GIT_TIMEOUT_SEC", "escaping_symlinks"]
