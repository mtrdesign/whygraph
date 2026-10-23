"""Tests for :mod:`whygraph.services.codegraph.symlinks` (plan Step 1, F5, R16).

CodeGraph follows a symbolic link out of the repository when it indexes,
so :func:`escaping_symlinks` must find every link CodeGraph would read
through - tracked or not, at any depth, relative or absolute, inside an
embedded repository or under a ``codegraph.json`` ``include`` - and nothing
CodeGraph never reads (a gitignored ``.venv/bin/python``). The fixtures
follow the security review's shapes (``spike/sec``): a repository beside
an ``outside`` folder holding the secrets. Every test runs the real ``git``.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from whygraph.core.safe_paths import resolves_inside
from whygraph.services.codegraph import (
    CodeGraphBootstrapError,
    CodeGraphUnsafeSymlinkError,
    escaping_symlinks,
    index_predates_guard,
    refresh_codegraph_index,
)
from whygraph.services.codegraph.symlinks import _include_roots

SECRET = "def SECRET_VALUE():\n    return 'leaked'\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=T",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.symlinks=true",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "app").mkdir()
    (path / "app" / "main.py").write_text("def main():\n    return 1\n")
    return path


def _commit(root: Path, message: str = "commit") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, Path]:
    """``(repo, outside)``: a fresh repository and a sibling folder of secrets."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text(SECRET)
    (outside / "pkg").mkdir()
    (outside / "pkg" / "mod.py").write_text(SECRET)
    return _repo(tmp_path / "repo"), outside


def test_a_clean_repository_has_none(world: tuple[Path, Path]) -> None:
    root, _ = world
    _commit(root)
    assert escaping_symlinks(root) == ()


def test_tracked_links_out_are_found_relative_absolute_and_nested(
    world: tuple[Path, Path],
) -> None:
    root, outside = world
    (root / "app" / "evil.py").symlink_to("../../outside/secret.py")
    (root / "app" / "abs.py").symlink_to(outside / "secret.py")
    (root / "app" / "deep" / "nested").mkdir(parents=True)
    (root / "app" / "deep" / "nested" / "leak.py").symlink_to(
        "../../../../outside/secret.py"
    )
    (root / "linkdir").symlink_to("../outside/pkg")
    _commit(root)

    assert escaping_symlinks(root) == (
        "app/abs.py",
        "app/deep/nested/leak.py",
        "app/evil.py",
        "linkdir",
    )


def test_an_in_root_link_is_allowed(world: tuple[Path, Path]) -> None:
    root, _ = world
    (root / "app" / "inroot.py").symlink_to("main.py")
    (root / "alias").symlink_to("app")
    (root / "app" / "later.py").symlink_to("not-yet.py")  # dangling, inside
    _commit(root)
    assert escaping_symlinks(root) == ()


def test_a_dangling_link_out_is_found(world: tuple[Path, Path]) -> None:
    root, _ = world
    (root / "app" / "gone.py").symlink_to("../../outside/missing.py")
    (root / "app" / "abs_gone.py").symlink_to("/nonexistent/whygraph/x.py")
    _commit(root)
    assert escaping_symlinks(root) == ("app/abs_gone.py", "app/gone.py")


def test_an_untracked_link_is_found_and_an_ignored_one_is_not(
    world: tuple[Path, Path],
) -> None:
    root, outside = world
    (root / ".gitignore").write_text(".venv/\n.env\n")
    _commit(root)
    (root / "app" / "untracked.py").symlink_to(outside / "secret.py")
    # The usual Python checkout: a gitignored venv whose python leaves the repo.
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").symlink_to(shutil.which("git") or "/bin/sh")
    (root / ".env").symlink_to(outside / "secret.py")

    assert escaping_symlinks(root) == ("app/untracked.py",)


def test_a_tracked_file_or_folder_replaced_by_a_link_is_found(
    world: tuple[Path, Path],
) -> None:
    """A typechange: the index still says file / folder, the disk says link."""
    root, outside = world
    (root / "lib").mkdir()
    (root / "lib" / "util.py").write_text("X = 1\n")
    _commit(root)
    (root / "app" / "main.py").unlink()
    (root / "app" / "main.py").symlink_to(outside / "secret.py")
    shutil.rmtree(root / "lib")
    (root / "lib").symlink_to(outside / "pkg")

    assert escaping_symlinks(root) == ("app/main.py", "lib")


def test_a_link_inside_an_embedded_repository_is_found(
    world: tuple[Path, Path],
) -> None:
    root, outside = world
    _commit(root)
    nested = _repo(root / "vendored")
    (nested / "app" / "leak.py").symlink_to(outside / "secret.py")
    _commit(nested)
    (nested / "loose.py").symlink_to(outside / "secret.py")  # untracked there

    assert escaping_symlinks(root) == (
        "vendored/app/leak.py",
        "vendored/loose.py",
    )


def test_include_ignored_reaches_an_embedded_repository_in_a_gitignored_folder(
    world: tuple[Path, Path],
) -> None:
    root, outside = world
    (root / ".gitignore").write_text("services/\n")
    _commit(root)
    nested = _repo(root / "services" / "api")
    (nested / "app" / "leak.py").symlink_to(outside / "secret.py")
    _commit(nested)
    assert escaping_symlinks(root) == ()  # CodeGraph skips a gitignored folder

    (root / "codegraph.json").write_text(json.dumps({"includeIgnored": ["services/"]}))
    assert escaping_symlinks(root) == ("services/api/app/leak.py",)


def test_include_walks_into_a_linked_folder(world: tuple[Path, Path]) -> None:
    """The ``spike/sec`` shape: ``include: ["linkdir/**"]`` over a gitignored link."""
    root, _ = world
    (root / ".gitignore").write_text("linkdir\n")
    (root / "codegraph.json").write_text(
        json.dumps({"include": ["linkdir/**"], "extensions": {".txt": "python"}})
    )
    _commit(root)
    (root / "linkdir").symlink_to("../outside/pkg")
    assert escaping_symlinks(root) == ("linkdir",)


def test_include_follows_an_in_root_folder_link_to_a_link_out(
    world: tuple[Path, Path],
) -> None:
    root, outside = world
    (root / ".gitignore").write_text("generated/\n")
    (root / "codegraph.json").write_text(json.dumps({"include": ["src/**/*.py"]}))
    (root / "src").mkdir()
    (root / "src" / "gen").symlink_to("../generated")  # in-root folder link
    _commit(root)
    (root / "generated").mkdir()
    (root / "generated" / "leak.py").symlink_to(outside / "secret.py")

    assert escaping_symlinks(root) == ("src/gen/leak.py",)


def test_a_whole_tree_include_prunes_default_ignored_folders(
    world: tuple[Path, Path],
) -> None:
    root, outside = world
    (root / ".gitignore").write_text(".venv/\nnode_modules/\nlegacy/\n")
    (root / "codegraph.json").write_text(json.dumps({"include": ["**/*.py"]}))
    _commit(root)
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").symlink_to(shutil.which("git") or "/bin/sh")
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "node_modules" / "x" / "index.py").symlink_to(outside / "secret.py")
    (root / "legacy").mkdir()
    (root / "legacy" / "old.py").symlink_to(outside / "secret.py")

    assert escaping_symlinks(root) == ("legacy/old.py",)


def test_a_codegraph_json_that_is_a_link_out_is_found(
    world: tuple[Path, Path],
) -> None:
    root, outside = world
    (outside / "codegraph.json").write_text(json.dumps({"include": ["x/**"]}))
    (root / ".gitignore").write_text("codegraph.json\n")
    _commit(root)
    (root / "codegraph.json").symlink_to(outside / "codegraph.json")
    assert escaping_symlinks(root) == ("codegraph.json",)


def test_a_malformed_codegraph_json_is_no_config(world: tuple[Path, Path]) -> None:
    root, _ = world
    (root / "codegraph.json").write_text("{not json")
    _commit(root)
    assert escaping_symlinks(root) == ()


@pytest.mark.parametrize(
    ("patterns", "roots"),
    [
        (["Tools/"], ["Tools/"]),
        (["Tools/**"], ["Tools/"]),
        (["src/local/file.ts"], ["src/local/"]),
        (["/a/b/*.py", "a/"], ["a/"]),
        (["a/b/**", "c/{x,y}/*.py"], ["a/b/", "c/"]),
        (["**/*.py", "a/"], [""]),
        (["file.py"], [""]),
    ],
)
def test_include_roots_mirror_codegraph(patterns: list[str], roots: list[str]) -> None:
    assert _include_roots(tuple(patterns)) == roots


def test_a_folder_that_is_not_a_git_repository_is_refused(tmp_path: Path) -> None:
    """CodeGraph would walk the whole folder, following every link: nothing is judged."""
    (tmp_path / "plain").mkdir()
    with pytest.raises(CodeGraphBootstrapError, match="not a git repository"):
        escaping_symlinks(tmp_path / "plain")


def test_a_git_ignored_subfolder_is_refused(world: tuple[Path, Path]) -> None:
    root, _ = world
    (root / ".gitignore").write_text("scratch/\n")
    _commit(root)
    (root / "scratch").mkdir()
    with pytest.raises(CodeGraphBootstrapError, match="git ignores it"):
        escaping_symlinks(root / "scratch")


def test_a_subfolder_of_a_repository_is_listed_with_git(
    world: tuple[Path, Path],
) -> None:
    root, outside = world
    (root / "app" / "evil.py").symlink_to(outside / "secret.py")
    _commit(root)
    assert escaping_symlinks(root / "app") == ("evil.py",)


def test_resolves_inside(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (root / "in").symlink_to("a")
    (root / "out").symlink_to(tmp_path)
    assert resolves_inside(root, root / "in")
    assert resolves_inside(root, root)
    assert not resolves_inside(root, root / "out")
    # Judged by where it lands: out/root leaves and comes back to the root.
    assert resolves_inside(root, root / "out" / "root")


def test_the_refresh_never_indexes_through_a_link(
    world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, outside = world
    (root / "app" / "evil.py").symlink_to(outside / "secret.py")
    _commit(root)
    ran: list[list[str]] = []
    monkeypatch.setattr(
        "whygraph.services.codegraph.bootstrap._run_codegraph",
        lambda project_root, args, **kw: ran.append(args) or "",
    )
    with pytest.raises(CodeGraphUnsafeSymlinkError, match="app/evil.py"):
        refresh_codegraph_index(root, capture=True)
    assert ran == []


def _rows_mentioning(db: Path, needle: str) -> int:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE name LIKE ? OR qualified_name LIKE ?",
            (f"%{needle}%", f"%{needle}%"),
        ).fetchone()[0]


@pytest.mark.skipif(shutil.which("codegraph") is None, reason="needs the codegraph CLI")
def test_real_codegraph_a_pre_guard_leak_is_rebuilt_away(
    world: tuple[Path, Path],
) -> None:
    """R16 end to end: an index built through a link is rebuilt once the link is gone."""
    root, outside = world
    (root / "app" / "evil.py").symlink_to(outside / "secret.py")
    _commit(root)
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env.update(DO_NOT_TRACK="1", CODEGRAPH_TELEMETRY="0", CODEGRAPH_NO_UPDATE_CHECK="1")
    # What an older WhyGraph did: index with the link in place (CodeGraph follows it).
    subprocess.run(
        ["codegraph", "init", "-i"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        timeout=300,
    )
    db = root / ".codegraph" / "codegraph.db"
    assert _rows_mentioning(db, "SECRET_VALUE") > 0  # the leak this step fixes
    assert index_predates_guard(root) is True

    # Today: refused while the link exists...
    with pytest.raises(CodeGraphUnsafeSymlinkError):
        refresh_codegraph_index(root, capture=True, env=env)
    assert _rows_mentioning(db, "SECRET_VALUE") > 0

    # ...and rebuilt from scratch on the first refresh after it is gone.
    (root / "app" / "evil.py").unlink()
    _commit(root, "remove the link")
    refresh_codegraph_index(root, capture=True, env=env)
    assert _rows_mentioning(db, "SECRET_VALUE") == 0
    assert _rows_mentioning(db, "main") > 0
    assert index_predates_guard(root) is False
