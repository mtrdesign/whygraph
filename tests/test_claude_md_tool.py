"""Tests for ``claude-md-tool.sh``, the deterministic half of ``/whygraph-claude-md``.

The script is POSIX sh with git, sed, awk and grep, so every case runs under
dash and ``bash --posix`` (each where present) against fixture repositories
built in ``tmp_path``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from whygraph.services.git.credentials import PROVIDER_KEY_PATTERN, TOKEN_PATTERN

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "src/whygraph/assets/claude-code/skills/whygraph-claude-md/claude-md-tool.sh"
)
_DRAFT = ".whygraph/claude-md/draft"

# Each shell present on the host (a copy of tests/test_plan_tool.py's table).
_SHELLS = {
    name: shell
    for name, shell in (("dash", ("/bin/dash",)), ("bash", ("bash", "--posix")))
    if shutil.which(shell[0])
}
_SHELL_IDS = sorted(_SHELLS)


@pytest.fixture(params=_SHELL_IDS)
def posix(request: pytest.FixtureRequest) -> str:
    """Each shell present on the host, as a test id."""
    return request.param


def _env() -> dict[str, str]:
    """The test environment: no ambient git state, no developer git config."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _git(cwd: Path, *args: str) -> str:
    run = subprocess.run(
        ["git", *args], cwd=cwd, env=_env(), capture_output=True, text=True, timeout=60
    )
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


def _run(cwd: Path, posix: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the script with no terminal, as Claude Code runs it."""
    return subprocess.run(
        [*_SHELLS[posix], str(_SCRIPT), *args],
        cwd=cwd,
        env=_env(),
        capture_output=True,
        text=True,
        timeout=120,
        start_new_session=True,
    )


def _commit(repo: Path, message: str = "change") -> None:
    _git(repo, "add", "--all")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository on ``main`` with a few tracked files that ignores ``.whygraph/``."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "tests@example.com")
    _git(root, "config", "user.name", "Tests")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text(".whygraph/\n")
    (root / "README.md").write_text("# Demo\n")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('hi')\n")
    (root / "scripts").mkdir()
    (root / "scripts" / "check.sh").write_text("true\n")
    _commit(root, "first")
    return root


# A root file in the §5.3 shape, every rule satisfied.
_VALID = """\
# Demo

A small demo app for tests. It optimizes for clarity.

<!-- whygraph:sources
1: user
-->

## Commands
- `uv run pytest -n0 tests/x.py::test_y` - one test, serially
- `sh scripts/check.sh` - the full check

<!-- whygraph:sources
1: manifest:pyproject.toml
2: ci:ci.yml
-->

## Boundaries
- Keep `src/app.py` free of network calls - the tests run offline.

<!-- whygraph:sources
1: note #14, PR #88
-->

## Where things live
- `README.md` - what the project is

<!-- whygraph:sources
1: user
-->
"""


def _draft(repo: Path, text: str, target: str = "CLAUDE.md") -> str:
    path = repo / _DRAFT / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return f"{_DRAFT}/{target}"


def _check(
    repo: Path, posix: str, text: str, kind: str = "root", target: str = "CLAUDE.md"
):
    return _run(repo, posix, "check", _draft(repo, text, target), kind)


def _rules(out: str, level: str = "fail") -> set[str]:
    return {
        line.split(":", 1)[0].split(" ", 1)[1]
        for line in out.splitlines()
        if line.startswith(level + " ")
    }


# ---- 1. inventory -----------------------------------------------------------


def test_inventory_lists_files_and_states(repo: Path, posix: str) -> None:
    (repo / "CLAUDE.md").write_text("# Demo\n<!-- c -->\nlong line here\n")
    (repo / "src" / "CLAUDE.md").write_text("- a\n")
    (repo / ".claude" / "rules").mkdir(parents=True)
    (repo / ".claude" / "CLAUDE.md").write_text("block\n")
    (repo / ".claude" / "rules" / "tests.md").write_text("rule\n")
    _commit(repo)
    (repo / "src" / "CLAUDE.md").write_text("- a\n- b\n")
    (repo / "AGENTS.md").write_text("agents\n")
    (repo / "CLAUDE.local.md").write_text("secret preference\n")

    run = _run(repo, posix, "inventory")

    assert run.returncode == 0, run.stderr
    rows = {r.split("\t")[0]: r.split("\t") for r in run.stdout.splitlines()}
    assert rows["CLAUDE.md"] == ["CLAUDE.md", "3", "33", "11", "14", "tracked"]
    assert rows["src/CLAUDE.md"][1:3] == ["2", "8"]
    assert rows["src/CLAUDE.md"][5] == "dirty"
    assert rows["AGENTS.md"][5] == "untracked"
    assert rows[".claude/CLAUDE.md"][5] == "tracked"
    assert rows[".claude/rules/tests.md"][5] == "tracked"
    assert rows["CLAUDE.local.md"] == [
        "CLAUDE.local.md",
        "-",
        "-",
        "-",
        "-",
        "untracked",
    ]
    assert "secret preference" not in run.stdout


# ---- 2. signals -------------------------------------------------------------


def test_signals_parts_caps_and_redaction(repo: Path, posix: str) -> None:
    (repo / "Makefile").write_text(
        "".join(f"t{i}:\n\techo {i}\n" for i in range(40)) + "VAR := 1\n.PHONY: t0\n"
    )
    scripts = ", ".join(f'"s{i}": "run {i}"' for i in range(50))
    (repo / "package.json").write_text('{"name": "x", "scripts": {' + scripts + "}}\n")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "pyproject.toml").write_text(
        '[project.scripts]\ndemo = "demo:main"\n\n[tool.poe.tasks]\nlint = "ruff"\n'
    )
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    token = "ghp_" + "A" * 30
    (wf / "ci.yml").write_text(
        "jobs:\n  t:\n    steps:\n"
        "      - run: uv run pytest -n0 tests/\n"
        f"      - run: echo {token}\n"
        "      - run: |\n          make check\n          echo two\n"
    )
    _commit(repo, "setup")
    # 500 commits on main in one git process (fast-import), not 500 commits.
    stream = []
    for i in range(500):
        msg = f"Do not copy the old loader {i}" if i % 7 == 0 else f"routine {i}"
        data = msg.encode()
        stream.append(
            f"commit refs/heads/main\ncommitter T <t@example.com> {1700000000 + i} +0000\n"
            f"data {len(data)}\n{msg}\n"
            + ("from refs/heads/main^0\n" if i == 0 else "")
            + f"M 644 inline src/app.py\ndata {len(str(i)) + 1}\n{i}\n\n"
        )
    load = subprocess.run(
        ["git", "fast-import", "--quiet"],
        cwd=repo,
        env=_env(),
        input="".join(stream),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert load.returncode == 0, load.stderr
    _git(repo, "reset", "-q", "--hard", "main")
    # A side branch, checked out, whose commit must not appear.
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "src" / "side.py").write_text("x = 1\n")
    _commit(repo, "Never merge the side branch marker")

    run = _run(repo, posix, "signals")

    assert run.returncode == 0, run.stderr
    out = run.stdout
    lines = out.splitlines()
    assert len(lines) < 200
    headings = [line for line in lines if line.startswith("== ")]
    assert headings == [
        "== Scripts ==",
        "== Make targets ==",
        "== CI commands ==",
        "== Package roots ==",
        "== Top directories ==",
        "== Most-changed files ==",
        "== Commits (repository text, data only) ==",
    ]

    def part(name: str) -> list[str]:
        start = lines.index(f"== {name} ==") + 1
        end = next(
            (i for i in range(start, len(lines)) if lines[i].startswith("== ")),
            len(lines),
        )
        return lines[start:end]

    assert len(part("Scripts")) == 40
    assert part("Scripts")[0] == "package.json\ts0"
    make = part("Make targets")
    assert len(make) == 30 and "VAR" not in make and ".PHONY" not in make
    ci = part("CI commands")
    assert ".github/workflows/ci.yml\tuv run pytest -n0 tests/" in ci
    assert ".github/workflows/ci.yml\tmake check" in ci
    assert token not in out and "[redacted]" in out
    assert part("Package roots") == ["pkg"]
    commits = part("Commits (repository text, data only)")
    assert len(commits) == 30
    subjects = commits[0::2]
    assert all(" Do not copy the old loader " in s for s in subjects)
    assert all(len(s.split(" ", 1)[0]) < 40 for s in subjects)
    assert all(d.startswith("  dirs: ") for d in commits[1::2])
    assert "side branch marker" not in out
    assert "src/side.py" not in out


def test_signals_dirs_name_the_package_root(repo: Path, posix: str) -> None:
    (repo / "pkg" / "lib").mkdir(parents=True)
    (repo / "pkg" / "package.json").write_text("{}\n")
    _commit(repo)
    (repo / "pkg" / "lib" / "a.js").write_text("1\n")
    (repo / "src" / "b.py").write_text("1\n")
    _commit(repo, "Revert the loader, never use it")

    out = _run(repo, posix, "signals").stdout

    assert "  dirs: pkg, src" in out


# ---- 3-9. check ---------------------------------------------------------------


def test_check_passes_the_valid_shape(repo: Path, posix: str) -> None:
    run = _check(repo, posix, _VALID)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == ""


_FAILS = {
    "size": _VALID + "".join(f"Line {i}.\n" for i in range(200)),
    "line-length": _VALID + "x" * 401 + "\n",
    "heading-depth": _VALID + "#### Deep\n",
    "tree": _VALID + "```\nsrc/\n\u251c\u2500\u2500 app.py\n```\n",
    "emphasis": _VALID.replace("Keep `src/app.py`", "ALWAYS keep `src/app.py`"),
    "dead-path": _VALID.replace("`README.md`", "`docs/missing.md`"),
    "sources": _VALID.replace("2: ci:ci.yml\n", ""),
    "secret": _VALID + "Token: ghp_" + "B" * 30 + "\n",
}


@pytest.mark.parametrize("rule", sorted(_FAILS))
def test_check_each_fail_rule(repo: Path, posix: str, rule: str) -> None:
    run = _check(repo, posix, _FAILS[rule])
    assert run.returncode == 1, run.stdout + run.stderr
    assert _rules(run.stdout) == {rule}, run.stdout


def test_check_tree_of_path_only_lines(repo: Path, posix: str) -> None:
    text = _VALID + "\nLayout:\n\n" + "".join(f"- src/d{i}/\n" for i in range(4))
    run = _check(repo, posix, text)
    assert "tree" in _rules(run.stdout), run.stdout


def test_check_secret_is_not_echoed(repo: Path, posix: str) -> None:
    run = _check(repo, posix, _FAILS["secret"])
    assert "B" * 30 not in run.stdout


def test_check_duplicate_rule(repo: Path, posix: str) -> None:
    _draft(repo, _VALID)
    nested = "## Commands\n- `sh scripts/check.sh` - the full check\n\n<!-- whygraph:sources\n1: user\n-->\n"
    run = _check(repo, posix, nested, "nested", "src/CLAUDE.md")
    assert run.returncode == 1
    assert _rules(run.stdout) == {"duplicate"}, run.stdout
    # The same bullet in the root draft is not a duplicate of itself.
    assert _check(repo, posix, _VALID).returncode == 0


@pytest.mark.parametrize(
    ("rule", "line"),
    [
        ("import", "@docs/guide.md\n"),
        ("section-name", "## Tech stack\nSome prose.\n"),
        ("managed-block", "Use codegraph_search first.\n"),
    ],
)
def test_check_each_warn_rule(repo: Path, posix: str, rule: str, line: str) -> None:
    run = _check(repo, posix, _VALID + line)
    assert run.returncode == 0, run.stdout
    assert _rules(run.stdout, "warn") == {rule}, run.stdout
    assert _rules(run.stdout) == set()


def test_check_warn_and_fail_exits_one(repo: Path, posix: str) -> None:
    run = _check(repo, posix, _VALID + "@x.md\n#### Deep\n")
    assert run.returncode == 1
    assert _rules(run.stdout, "warn") == {"import"}
    assert _rules(run.stdout) == {"heading-depth"}


def test_dead_path_skips_and_resolution(repo: Path, posix: str) -> None:
    (repo / "notes.md").write_text("untracked but present\n")
    (repo / "src" / "local.py").write_text("1\n")
    _commit(repo)
    root = _VALID + (
        "\nSee `<placeholder>/x.md`, `src/**/*.py`, `$HOME/x`, `https://x/y`, "
        "`notes.md` and `uv run pytest tests/x.py::t`.\n"
    )
    run = _check(repo, posix, root)
    assert run.returncode == 0, run.stdout
    nested = "## Traps\n- `local.py` and `./local.py` - read both.\n\n<!-- whygraph:sources\n1: user\n-->\n"
    run = _check(repo, posix, nested, "nested", "src/CLAUDE.md")
    assert run.returncode == 0, run.stdout


def test_size_ignores_comments_and_foreign_blocks(repo: Path, posix: str) -> None:
    foreign = (
        "\n<!-- CODEGRAPH_START -->\n"
        + "".join(f"IMPORTANT line {i} `missing/{i}.md`\n" for i in range(250))
        + "<!-- CODEGRAPH_END -->\n"
    )
    text = _VALID + "\n" + "<!-- a note\n" + "x\n" * 300 + "-->\n" + foreign
    run = _check(repo, posix, text)
    assert run.returncode == 0, run.stdout
    assert any(
        line.startswith("info foreign-block: CODEGRAPH ")
        for line in run.stdout.splitlines()
    ), run.stdout
    with_secret = text.replace("IMPORTANT line 3", "ghp_" + "C" * 30)
    assert _rules(_check(repo, posix, with_secret).stdout) == {"secret"}


@pytest.mark.parametrize(
    "broken",
    [
        _VALID.replace("2: ci:ci.yml\n", "2: ci:ci.yml\n3: user\n"),  # wrong count
        _VALID.replace(
            "\n<!-- whygraph:sources\n1: note", "<!-- whygraph:sources\n1: note"
        ),
        _VALID.replace("1: note #14, PR #88", "1: note -- 14"),
        _VALID.replace("1: note #14, PR #88", "1: note > 14"),
    ],
    ids=["count", "blank-line", "double-dash", "bad-char"],
)
def test_sources_rule(repo: Path, posix: str, broken: str) -> None:
    run = _check(repo, posix, broken)
    assert run.returncode == 1
    assert _rules(run.stdout) == {"sources"}, run.stdout


# ---- 10-12. path refusals, apply, draft-dir ---------------------------------


def _symlinked_file(repo: Path) -> str:
    (repo / "elsewhere.md").write_text(_VALID)
    os.symlink(repo / "elsewhere.md", repo / "src" / "CLAUDE.md")
    return "src/CLAUDE.md"


def _symlinked_parent(repo: Path) -> str:
    os.symlink(repo / "src", repo / "linked")
    return "linked/CLAUDE.md"


def _submodule(repo: Path) -> str:
    sub = repo.parent / "sub"
    sub.mkdir()
    _git(sub, "init", "-q", "-b", "main")
    _git(sub, "config", "user.email", "t@example.com")
    _git(sub, "config", "user.name", "T")
    _git(sub, "config", "commit.gpgsign", "false")
    (sub / "f.txt").write_text("f\n")
    _commit(sub)
    _git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(sub),
        "vendor",
    )
    _commit(repo, "add submodule")
    return "vendor/CLAUDE.md"


_REFUSED = {
    "outside": lambda repo: "../x/CLAUDE.md",
    "symlinked-file": _symlinked_file,
    "symlinked-parent": _symlinked_parent,
    "claude-dir": lambda repo: ".claude/CLAUDE.md",
    "agents": lambda repo: "AGENTS.md",
    "rules": lambda repo: ".claude/rules/x.md",
    "newline": lambda repo: "src\n/CLAUDE.md",
    "submodule": _submodule,
}


@pytest.mark.parametrize("case", sorted(_REFUSED))
def test_check_and_apply_refuse(repo: Path, posix: str, case: str) -> None:
    target = _REFUSED[case](repo)
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / "CLAUDE.md").write_text("block\n")
    if case in ("outside", "newline"):
        paths = [target, f"{_DRAFT}/{target}"]
    else:
        paths = [target]
        draft = repo / _DRAFT / target
        try:
            draft.parent.mkdir(parents=True, exist_ok=True)
            draft.write_text(_VALID)
            paths.append(f"{_DRAFT}/{target}")
        except OSError:
            pass
    for path in paths:
        # A refusal comes before the kind is judged, so the kind is moot.
        check = _run(repo, posix, "check", path, "nested")
        assert check.returncode == 3, (path, check.stdout, check.stderr)
        if path.startswith(_DRAFT):
            apply = _run(repo, posix, "apply", path)
            assert apply.returncode == 3, (path, apply.stdout, apply.stderr)
    assert (repo / ".claude" / "CLAUDE.md").read_text() == "block\n"


def _case_insensitive(path: Path) -> bool:
    probe = path / "CaseProbe"
    probe.write_text("x")
    try:
        return (path / "caseprobe").exists()
    finally:
        probe.unlink()


def test_case_variant_of_claude_dir_is_refused(repo: Path, posix: str) -> None:
    if not _case_insensitive(repo):
        pytest.skip("case-sensitive file system")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "CLAUDE.md").write_text("block\n")
    _commit(repo)
    draft = _draft(repo, _VALID, ".Claude/CLAUDE.md")
    for args in (("check", draft, "nested"), ("apply", draft)):
        run = _run(repo, posix, *args)
        assert run.returncode == 3, run.stdout + run.stderr
        assert "the same file as .claude/CLAUDE.md" in run.stderr
    assert (repo / ".claude" / "CLAUDE.md").read_text() == "block\n"


def test_apply_writes_backs_up_and_if_clean(repo: Path, posix: str) -> None:
    (repo / "CLAUDE.md").write_text("# Old\n")
    _commit(repo)
    draft = _draft(repo, _VALID)

    run = _run(repo, posix, "apply", draft, "--if-clean")

    assert run.returncode == 0, run.stderr
    out = run.stdout.splitlines()
    assert out[0] == "wrote CLAUDE.md"
    assert out[1].startswith("backup .whygraph/claude-md/backup/")
    assert (repo / out[1].split(" ", 1)[1]).read_text() == "# Old\n"
    assert (repo / "CLAUDE.md").read_text() == _VALID
    assert not (repo / "CLAUDE.md.whygraph-tmp").exists()

    # Now dirty (the applied text is uncommitted): --if-clean refuses.
    assert _run(repo, posix, "apply", draft, "--if-clean").returncode == 4
    # Untracked target.
    (repo / "src" / "CLAUDE.md").write_text("mine\n")
    nested = _draft(
        repo,
        "## Traps\n- `app.py` - prints.\n\n<!-- whygraph:sources\n1: user\n-->\n",
        "src/CLAUDE.md",
    )
    assert _run(repo, posix, "apply", nested, "--if-clean").returncode == 4
    assert (repo / "src" / "CLAUDE.md").read_text() == "mine\n"
    # Without --if-clean, both write after a backup.
    run = _run(repo, posix, "apply", nested)
    assert run.returncode == 0, run.stderr
    assert "backup " in run.stdout


def test_apply_new_file_and_failing_draft(repo: Path, posix: str) -> None:
    run = _run(repo, posix, "apply", _draft(repo, _FAILS["emphasis"]))
    assert run.returncode == 1
    assert not (repo / "CLAUDE.md").exists()
    run = _run(repo, posix, "apply", _draft(repo, _VALID))
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == "wrote CLAUDE.md\n"


def test_apply_refuses_a_path_outside_the_draft_dir(repo: Path, posix: str) -> None:
    (repo / "CLAUDE.md").write_text(_VALID)
    assert _run(repo, posix, "apply", "CLAUDE.md").returncode == 3


def test_apply_refuses_a_directory_without_tracked_files(
    repo: Path, posix: str
) -> None:
    (repo / "empty").mkdir()
    draft = _draft(
        repo,
        "## Traps\n- x - y.\n\n<!-- whygraph:sources\n1: user\n-->\n",
        "empty/CLAUDE.md",
    )
    assert _run(repo, posix, "apply", draft).returncode == 3


def test_draft_dir_and_clean(repo: Path, posix: str) -> None:
    assert _run(repo, posix, "draft-dir").returncode == 0
    assert (repo / _DRAFT).is_dir()
    (repo / _DRAFT / "CLAUDE.md").write_text("x\n")
    (repo / ".whygraph/claude-md/backup").mkdir()
    (repo / ".whygraph/claude-md/backup/keep").write_text("k\n")
    (repo / ".whygraph/claude-md/proposed.txt").write_text("decision\tsrc/app.py\n")

    assert _run(repo, posix, "clean").returncode == 0

    assert list((repo / _DRAFT).iterdir()) == []
    assert (repo / ".whygraph/claude-md/backup/keep").exists()
    assert (repo / ".whygraph/claude-md/proposed.txt").exists()


def test_draft_dir_refuses_a_tracked_symlink(repo: Path, posix: str) -> None:
    (repo / ".gitignore").write_text("")
    (repo / ".whygraph").mkdir()
    (repo / "target").mkdir()
    (repo / "target" / "f").write_text("f\n")
    os.symlink(repo / "target", repo / ".whygraph" / "claude-md")
    _commit(repo, "track the symlink")
    assert ".whygraph/claude-md" in _git(repo, "ls-files")
    for cmd in ("draft-dir", "clean"):
        run = _run(repo, posix, cmd)
        assert run.returncode == 3, (cmd, run.stderr)
    assert (repo / "target" / "f").exists()


# ---- 13. usage --------------------------------------------------------------


def test_usage_errors(repo: Path, posix: str) -> None:
    root_draft = _draft(repo, _VALID)
    nested = _draft(repo, _VALID, "src/CLAUDE.md")
    assert _run(repo, posix, "check", root_draft, "nested").returncode == 2
    assert _run(repo, posix, "check", nested, "root").returncode == 2
    assert _run(repo, posix, "check", root_draft, "other").returncode == 2
    assert _run(repo, posix, "nope").returncode == 2
    assert _run(repo, posix).returncode == 2


# ---- 14. secret shapes --------------------------------------------------------

_SECRETS = {
    "gh": "ghp_" + "a" * 20,
    "wgc": "wgc_" + "b" * 20,
    "sk": "key sk-" + "c" * 24,
    "github_pat": "github_pat_" + "d" * 24,
    "aws": "AKIA" + "E" * 16,
    "slack": "xoxb-" + "1" * 12,
    "pem": "-----BEGIN RSA PRIVATE KEY-----",
    "url": "https://user:pass@example.com/x",
}


@pytest.mark.parametrize("name", sorted(_SECRETS))
def test_secret_shapes(repo: Path, posix: str, name: str) -> None:
    sample = _SECRETS[name]
    run = _check(repo, posix, _VALID + f"\nValue {sample} here.\n")
    assert run.returncode == 1
    assert _rules(run.stdout) == {"secret"}, run.stdout
    if name in ("gh", "wgc"):
        assert TOKEN_PATTERN.search(sample)
    if name == "sk":
        assert PROVIDER_KEY_PATTERN.search(sample)


def test_secret_redaction_in_signals_matches_python(repo: Path, posix: str) -> None:
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "".join(f"      - run: echo {i} {s}\n" for i, s in enumerate(_SECRETS.values()))
    )
    _commit(repo)
    out = _run(repo, posix, "signals").stdout
    for name, sample in _SECRETS.items():
        secret = sample.split(" ")[-1] if name != "pem" else sample
        if name == "url":
            secret = "user:pass@"
        assert secret not in out, (name, out)
