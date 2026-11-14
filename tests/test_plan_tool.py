"""Tests for ``plan-tool.sh``, the deterministic half of the planning harness.

The script is POSIX sh and POSIX awk, so these tests run it through ``sh`` with
the host's own ``awk`` and ``git`` (BSD awk on macOS, mawk or gawk in CI). The
byte identity of its four copies is pinned in ``tests/test_assets.py``.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "src/whygraph/assets/claude-code/skills/whygraph-plan/plan-tool.sh"
)
_GUARD = _SCRIPT.parent / "guard.sh"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_EM = "\u2014"
_EN = "\u2013"
# `sh missing.sh` exits 127 under bash's sh (macOS) and 2 under dash (Linux CI).
_SH_MISSING = str(
    subprocess.run(["sh", "./no-such-script.sh"], capture_output=True).returncode
)

_CLEAN_PLAN = f"""\
Note: Auto-mode chose single-pass.

# Plan: Add a scan limit

**Status:** PROPOSAL - nothing implemented yet.
**Objective:** Add a scan limit.
**Non-goals:** none

## Working set
1 symbol analyzed.

## Blockers

- **Constraint at risk:** "a verbatim quote {_EM} with a dash"

## Steps

1. **Add the limit**
   - **Files:** src/a.py:1-2
   - **Change:** Read the limit in `estimate_cost`. Pass the limit to the caller.
   - **Boundaries:** none
   - **Constraints to preserve:** "stay fast {_EM} always"
   - **Risks:** "may slow {_EN} sometimes"
   - **Test first:** none
   - **Model:** sonnet - copies a sibling
   - **Routes:** `/settings` `/a/b.c`
   - **Expected UI:** The page shows the limit.
   - **Verify:** `true` and `true`
2. **Show the limit**
   - **Files:** src/b.py:3
   - **Change:** Show the limit.
   - **Boundaries:** none
   - **Constraints to preserve:** none recorded
   - **Risks:** none recorded
   - **Test first:** none
   - **Model:** opus - new design
   - **Verify:** manual: look at the page

## Risks called out across the change
- "a risk {_EM} quoted"

## Acceptance
1. The suite passes - **Check:** `true`
2. The page shows the limit - **Check:** manual: open the page

## Open questions
- **Q1.** Which limit applies? **Options:** ten / twenty **Default:** ten **Plan-shaping:** yes - it changes the steps

## Confidence: high
A reason.
"""


def _env() -> dict[str, str]:
    """The test environment: no ambient git state, no developer git config."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _tool(
    cwd: Path,
    *args: str,
    shell: tuple[str, ...] = ("sh",),
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the script with no terminal, as Claude Code runs it."""
    return subprocess.run(
        [*shell, str(_SCRIPT), *args],
        cwd=cwd,
        env={**_env(), **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
        start_new_session=True,
    )


def _git(cwd: Path, *args: str, stdin: str | None = None) -> str:
    run = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_env(),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository with one commit that ignores ``.whygraph/``, as a set-up project does."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.com")
    _git(root, "config", "user.name", "Tests")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text(".whygraph/\nplans/\n")
    (root / "a.txt").write_text("alpha\n")
    _git(root, "add", "--all")
    _git(root, "commit", "-q", "-m", "first")
    return root


# ---- usage ----------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("nonsense",),
        ("stem",),
        ("stem", "a.md", "b.md"),
        ("snapshot", "extra"),
        ("fingerprint",),
        ("check",),
        ("check", "does-not-exist.md"),
        ("verify", "plan.md"),
        ("verify", "does-not-exist.md", "1"),
        ("verify", "plan.md", "x"),
        ("verify", "plan.md", "0"),
        ("verify", "plan.md", "99"),
        ("begin", "plan.md"),
        ("begin", "plan.md", "1", "2"),
        ("begin", "does-not-exist.md", "1"),
        ("begin", "plan.md", "x"),
        ("begin", "plan.md", "0"),
        ("begin", "plan.md", "99"),
        ("end", "plan.md", "1"),
        ("end", "plan.md", "1", "x" * 40),
        ("end", "plan.md", "1", "A" * 40),
        ("end", "plan.md", "1", "a" * 39),
        ("end", "plan.md", "1", "a" * 40),
        ("end", "plan.md", "x", "a" * 40),
        ("end", "does-not-exist.md", "1", "a" * 40),
        ("end", "plan.md", "1", "a" * 40, "blob"),
        ("end", "plan.md", "1", "a" * 40, "b" * 40, "extra"),
        ("pass", "plan.md"),
        ("pass", "plan.md", "1"),
        ("pass", "plan.md", "0"),
        ("pass", "does-not-exist.md", "1"),
        ("next",),
        ("next", "plan.md", "1"),
        ("next", "does-not-exist.md"),
        ("changed",),
        ("changed", "a" * 40),
        ("changed", "x", "y"),
        ("changed", "a" * 40, "b" * 40),
        ("changed", "a" * 40, "b" * 40, "c" * 40),
        ("probe",),
        ("probe", "plan.md", "1"),
        ("probe", "does-not-exist.md"),
        ("baseline", "plan.md"),
        ("baseline", "plan.md", "x"),
        ("baseline", "plan.md", "0"),
        ("baseline", "plan.md", "99"),
        ("baseline", "does-not-exist.md", "1"),
        ("baseline", "plan.md", "1", "second"),
        ("baseline", "plan.md", "1", "first", "blob"),
        ("baseline", "plan.md", "1", "a" * 40, "first"),
        ("baseline", "plan.md", "1", "first", "a" * 40, "extra"),
        ("accept", "plan.md"),
        ("accept", "plan.md", "0"),
        ("accept", "plan.md", "01"),
        ("accept", "plan.md", "x"),
        ("accept", "plan.md", "3"),
        ("accept", "does-not-exist.md", "1"),
        ("accept", "plan.md", "1", "blob"),
        ("accept", "plan.md", "1", "a" * 40, "extra"),
        ("attempt", "plan.md"),
        ("attempt", "plan.md", "1"),
        ("attempt", "plan.md", "0", "constraint"),
        ("attempt", "plan.md", "99", "constraint"),
        ("attempt", "plan.md", "1", "nope"),
        ("attempt", "plan.md", "1", "retry"),
        ("attempt", "plan.md", "1", "retry", "a>b"),
        ("attempt", "plan.md", "1", "findings", "3"),
        ("attempt", "plan.md", "1", "constraint", "x"),
        ("attempt", "plan.md", "1", "retry", "a:b", "c"),
        ("attempt", "does-not-exist.md", "1", "constraint"),
        ("amends", "plan.md"),
        ("amends", "plan.md", "x"),
        ("amends", "plan.md", "99"),
        ("amends", "plan.md", "1", "blob"),
        ("amends", "plan.md", "1", "a" * 40, "extra"),
        ("amends", "does-not-exist.md", "1"),
        ("save",),
        ("save", "plan.md", "x"),
        ("save", "does-not-exist.md"),
        ("restore", "plan.md"),
        ("restore", "plan.md", "blob"),
        ("restore", "plan.md", "a" * 40, "extra"),
        ("restore", "no-dir/plan.md", "a" * 40),
        ("log-check", "plan.md"),
        ("log-check", "plan.md", "blob"),
        ("log-check", "plan.md", "a" * 40, "extra"),
        ("state",),
        ("state", "plan.md", "x"),
        ("state", "plan.md", "a" * 40, "extra"),
        ("amend-check", "plan.md", "a" * 40),
        ("amend-check", "plan.md", "blob", "1"),
        ("amend-check", "plan.md", "a" * 40, "x"),
        ("amend-check", "plan.md", "a" * 40, "0"),
        ("amend-check", "plan.md", "a" * 40, "1", "extra"),
        ("amend-check", "does-not-exist.md", "a" * 40, "1"),
        ("constraints",),
        ("constraints", "does-not-exist.md"),
        ("constraints", "does-not-exist.md", "1"),
        ("constraints", "plan.md", "x"),
        ("constraints", "plan.md", "0"),
        ("constraints", "plan.md", "01"),
        ("constraints", "plan.md", "99"),
        ("constraints", "plan.md", "blob"),
        ("constraints", "plan.md", "1", "a" * 40),
        ("constraints", "plan.md", "a" * 40, "1", "extra"),
        ("constraints", "plan.md", "a" * 40, "x"),
    ],
)
def test_usage_errors_exit_64(repo: Path, args: tuple[str, ...]) -> None:
    (repo / "plan.md").write_text(_CLEAN_PLAN)
    run = _tool(repo, *args)
    assert run.returncode == 64, run.stdout + run.stderr
    assert run.stdout == ""
    assert run.stderr


# ---- stem -----------------------------------------------------------------


def _prose_stem(path: str) -> str:
    """The ``<stem>`` rule as the agent prose states it."""
    name = path.rsplit("/", 1)[-1].removesuffix(".md")
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("My_Plan.md", "my-plan"),
        ("plans/47 Harness--Hardening.v2.md", "47-harness-hardening-v2"),
        ("/abs/__Odd.Name__.txt", "odd-name-txt"),
        ("UPPER.MD", "upper-md"),
    ],
)
def test_stem_equals_the_prose_rule(tmp_path: Path, path: str, expected: str) -> None:
    run = _tool(tmp_path, "stem", path)
    assert run.returncode == 0, run.stderr
    assert run.stdout == f"{expected}\n"
    assert expected == _prose_stem(path)


# ---- snapshot -------------------------------------------------------------


def test_snapshot_equals_write_tree_and_leaves_the_index(repo: Path) -> None:
    (repo / "a.txt").write_text("changed\n")
    (repo / "new.txt").write_text("untracked\n")
    index = repo / ".git" / "index"
    before = index.read_bytes()
    status = _git(repo, "status", "--porcelain")

    run = _tool(repo, "snapshot")

    assert run.returncode == 0, run.stderr
    tree = run.stdout.strip()
    assert _HEX40.match(tree)
    assert index.read_bytes() == before
    assert _git(repo, "status", "--porcelain") == status
    assert _git(repo, "stash", "list") == ""

    _git(repo, "add", "--all")
    assert _git(repo, "write-tree") == tree


def test_snapshot_outside_a_repo_returns_the_failure(tmp_path: Path) -> None:
    run = _tool(tmp_path, "snapshot")
    assert run.returncode != 0
    assert run.stdout == ""


# ---- fingerprint ----------------------------------------------------------


def test_fingerprint_is_the_hash_of_the_lines_in_order(repo: Path) -> None:
    (repo / "b.txt").write_text("beta\n")
    a_hash = _git(repo, "hash-object", "a.txt")
    b_hash = _git(repo, "hash-object", "b.txt")
    lines = f"{a_hash} a.txt\n{b_hash} b.txt\nmissing nope.txt\n"
    expected = _git(repo, "hash-object", "--stdin", stdin=lines)

    first = _tool(repo, "fingerprint", "a.txt; b.txt; nope.txt")
    second = _tool(repo, "fingerprint", "a.txt; b.txt; nope.txt")

    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == expected
    assert second.stdout == first.stdout
    assert _tool(repo, "fingerprint", "b.txt; a.txt; nope.txt").stdout != first.stdout


def test_fingerprint_changes_with_the_file(repo: Path) -> None:
    before = _tool(repo, "fingerprint", "a.txt").stdout
    (repo / "a.txt").write_text("changed\n")
    assert _tool(repo, "fingerprint", "a.txt").stdout != before


@pytest.mark.parametrize(
    "unsafe",
    [
        "/etc/hostname",
        "-a.txt",
        "sub/../a.txt",
        "..",
        "../a.txt",
        "sub/..",
        "with space.txt",
        "semi;colon.txt",
        "$(touch pwned).txt",
    ],
)
def test_fingerprint_counts_an_unsafe_path_as_missing(repo: Path, unsafe: str) -> None:
    (repo / "sub").mkdir()
    (repo / "-a.txt").write_text("dash\n")
    (repo / "with space.txt").write_text("space\n")
    (repo / "semi;colon.txt").write_text("semicolon\n")
    a_hash = _git(repo, "hash-object", "a.txt")
    expected = _git(
        repo, "hash-object", "--stdin", stdin=f"{a_hash} a.txt\nmissing {unsafe}\n"
    )

    run = _tool(repo, "fingerprint", f"a.txt; {unsafe}")

    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == expected
    assert not (repo / "pwned").exists()


def test_fingerprint_counts_a_directory_as_missing(repo: Path) -> None:
    (repo / "sub").mkdir()
    expected = _git(repo, "hash-object", "--stdin", stdin="missing sub\n")
    assert _tool(repo, "fingerprint", "sub").stdout.strip() == expected


def test_fingerprint_paths_are_repo_relative(repo: Path) -> None:
    (repo / "sub").mkdir()
    assert (
        _tool(repo / "sub", "fingerprint", "a.txt").stdout
        == _tool(repo, "fingerprint", "a.txt").stdout
    )


# ---- check ----------------------------------------------------------------


def _check(tmp_path: Path, text: str) -> subprocess.CompletedProcess[str]:
    plan = tmp_path / "plan.md"
    plan.write_text(text, encoding="utf-8")
    return _tool(tmp_path, "check", str(plan))


def _line_of(text: str, needle: str) -> int:
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    raise AssertionError(f"not in the plan: {needle}")


def _replaced(old: str, new: str) -> str:
    assert old in _CLEAN_PLAN, old
    return _CLEAN_PLAN.replace(old, new, 1)


def _dropped(needle: str) -> str:
    kept = [line for line in _CLEAN_PLAN.splitlines() if needle not in line]
    assert len(kept) == len(_CLEAN_PLAN.splitlines()) - 1, needle
    return "\n".join(kept) + "\n"


def _assert_one_finding(tmp_path: Path, text: str, line: int, message: str) -> None:
    run = _check(tmp_path, text)
    assert run.returncode == 1, run.stdout + run.stderr
    findings = run.stdout.splitlines()
    assert len(findings) == 1, findings
    assert findings[0].startswith(f"line {line}: "), findings
    assert message in findings[0], findings


def test_check_clean_plan(tmp_path: Path) -> None:
    run = _check(tmp_path, _CLEAN_PLAN)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == ""


def test_check_accepts_a_path_with_an_equals_sign(tmp_path: Path) -> None:
    # awk would read `a=b.md` as a variable assignment, not a file.
    (tmp_path / "a=b.md").write_text(_CLEAN_PLAN, encoding="utf-8")
    assert _tool(tmp_path, "check", "a=b.md").returncode == 0


@pytest.mark.parametrize(
    ("needle", "marker"),
    [
        ("# Plan:", "# Plan:"),
        ("**Status:**", "**Status:**"),
        ("**Objective:**", "**Objective:**"),
        ("**Non-goals:**", "**Non-goals:**"),
        ("## Acceptance", "## Acceptance"),
    ],
)
def test_check_structure_marker_missing(
    tmp_path: Path, needle: str, marker: str
) -> None:
    # `## Acceptance` lost: its items are then plain text under the previous section.
    _assert_one_finding(tmp_path, _dropped(needle), 1, f"missing {marker}")


def test_check_structure_steps_missing(tmp_path: Path) -> None:
    # Without the heading the numbered blocks are no longer steps.
    text = _replaced("## Steps\n", "## Work\n")
    _assert_one_finding(tmp_path, text, 1, "missing ## Steps")


def test_check_structure_confidence_missing(tmp_path: Path) -> None:
    text = _replaced("## Confidence: high\n", "## Trust\n")
    _assert_one_finding(tmp_path, text, 1, "missing ## Confidence:")


@pytest.mark.parametrize(
    ("field", "needle"),
    [
        ("Files", "**Files:** src/a.py:1-2"),
        ("Change", "**Change:** Read the limit"),
        ("Boundaries", "**Boundaries:** none"),
        ("Constraints to preserve", '**Constraints to preserve:** "stay'),
        ("Risks", '**Risks:** "may'),
        ("Test first", "**Test first:** none"),
        ("Model", "**Model:** sonnet"),
        ("Verify", "**Verify:** `true`"),
    ],
)
def test_check_step_field_missing(tmp_path: Path, field: str, needle: str) -> None:
    text = _CLEAN_PLAN.replace(
        next(line + "\n" for line in _CLEAN_PLAN.splitlines() if needle in line), "", 1
    )
    assert text != _CLEAN_PLAN
    _assert_one_finding(
        tmp_path, text, _line_of(text, "1. **Add the limit**"), f"missing {field}"
    )


@pytest.mark.parametrize("word", ["gpt4", "sonnet,", "- sonnet", ""])
def test_check_model_first_word(tmp_path: Path, word: str) -> None:
    text = _replaced("**Model:** sonnet - copies a sibling", f"**Model:** {word}")
    _assert_one_finding(tmp_path, text, _line_of(text, "**Model:** " + word), "Model")


@pytest.mark.parametrize("word", ["haiku", "sonnet", "opus", "fable", "Opus"])
def test_check_model_accepts_the_four_tags(tmp_path: Path, word: str) -> None:
    text = _replaced("**Model:** sonnet - copies", f"**Model:** {word} - copies")
    assert _check(tmp_path, text).returncode == 0


@pytest.mark.parametrize(
    "verify",
    ["run the tests", "`grep x f` returns `x`", "`true", "`true`."],
)
def test_check_verify_not_literal(tmp_path: Path, verify: str) -> None:
    text = _replaced("**Verify:** `true` and `true`", f"**Verify:** {verify}")
    _assert_one_finding(
        tmp_path, text, _line_of(text, "**Verify:** " + verify), "Verify"
    )


@pytest.mark.parametrize(
    "verify",
    ["`true`", "`a`, `b`", "`a` then `b`", "`a` && `b`", "manual: look at it"],
)
def test_check_verify_accepts(tmp_path: Path, verify: str) -> None:
    text = _replaced("**Verify:** `true` and `true`", f"**Verify:** {verify}")
    assert _check(tmp_path, text).returncode == 0


@pytest.mark.parametrize(
    "route", ["`settings`", "`/a b`", "`/a?x=1`", "`/a;b`", "`/ok` `bad`", "/settings"]
)
def test_check_routes(tmp_path: Path, route: str) -> None:
    text = _replaced("**Routes:** `/settings` `/a/b.c`", f"**Routes:** {route}")
    _assert_one_finding(tmp_path, text, _line_of(text, "**Routes:**"), "Routes")


def test_check_routes_accepts_the_allowed_characters(tmp_path: Path) -> None:
    text = _replaced(
        "**Routes:** `/settings` `/a/b.c`", "**Routes:** `/` `/a_b/c.d~e%20-f`"
    )
    assert _check(tmp_path, text).returncode == 0


def test_check_acceptance_item_without_check(tmp_path: Path) -> None:
    text = _replaced(
        "2. The page shows the limit - **Check:** manual: open the page",
        "2. The page shows the limit",
    )
    _assert_one_finding(
        tmp_path, text, _line_of(text, "2. The page shows"), "**Check:**"
    )


def test_check_acceptance_numbers_are_not_steps(tmp_path: Path) -> None:
    # A bold statement in Acceptance has the shape of a step header.
    text = _replaced("1. The suite passes -", "1. **The suite** passes -")
    assert _check(tmp_path, text).returncode == 0


_YES = "**Options:** a / b **Default:** a **Plan-shaping:** yes - why"


def test_check_three_plan_shaping_questions_pass(tmp_path: Path) -> None:
    extra = "".join(f"- **Q{n}.** Which one? {_YES}\n" for n in (2, 3))
    text = _replaced("\n## Confidence: high", f"{extra}\n## Confidence: high")
    assert _check(tmp_path, text).returncode == 0


def test_check_more_than_three_plan_shaping_questions(tmp_path: Path) -> None:
    extra = "".join(f"- **Q{n}.** Which one? {_YES}\n" for n in (2, 3, 4))
    text = _replaced("\n## Confidence: high", f"{extra}\n## Confidence: high")
    _assert_one_finding(tmp_path, text, _line_of(text, "**Q4.**"), "Plan-shaping")


@pytest.mark.parametrize("label", ["Options", "Default"])
def test_check_question_without_options_or_default(tmp_path: Path, label: str) -> None:
    text = _replaced(f"**{label}:**", "**Other:**")
    _assert_one_finding(tmp_path, text, _line_of(text, "**Q1.**"), label)


_WORDS_20 = " ".join(["word"] * 20)
_WORDS_21 = " ".join(["word"] * 21)


@pytest.mark.parametrize(
    ("old", "template", "needle"),
    [
        (
            "**Change:** Read the limit in `estimate_cost`. Pass the limit to the caller.",
            "**Change:** {}.",
            "**Change:**",
        ),
        (
            "**Expected UI:** The page shows the limit.",
            "**Expected UI:** {}.",
            "**Expected UI:**",
        ),
        (
            "1. The suite passes - **Check:** `true`",
            "1. {} - **Check:** `true`",
            "1. word",
        ),
        (
            "- **Q1.** Which limit applies? **Options:**",
            "- **Q1.** {}? **Options:**",
            "**Q1.**",
        ),
    ],
)
def test_check_sentence_length(
    tmp_path: Path, old: str, template: str, needle: str
) -> None:
    fits = _replaced(old, template.format(_WORDS_20))
    assert _check(tmp_path, fits).returncode == 0, _check(tmp_path, fits).stdout

    long = _replaced(old, template.format(_WORDS_21))
    _assert_one_finding(tmp_path, long, _line_of(long, needle), "over 20 words")


def _change_is(text: str) -> str:
    return _replaced(
        "**Change:** Read the limit in `estimate_cost`. Pass the limit to the caller.",
        f"**Change:** {text}",
    )


def test_check_a_backticked_span_is_one_word(tmp_path: Path) -> None:
    span = "`many words. inside; this one span? yes! it does not split`"
    fits = _change_is(f"{span} {' '.join(['word'] * 19)}.")
    assert _check(tmp_path, fits).returncode == 0, _check(tmp_path, fits).stdout

    long = _change_is(f"{span} {' '.join(['word'] * 20)}.")
    _assert_one_finding(tmp_path, long, _line_of(long, "**Change:**"), "over 20 words")


@pytest.mark.parametrize("stop", [".", "?", "!", ";"])
def test_check_sentences_split_on_each_terminator(tmp_path: Path, stop: str) -> None:
    text = _change_is(f"{_WORDS_20}{stop} {_WORDS_20}.")
    assert _check(tmp_path, text).returncode == 0, _check(tmp_path, text).stdout


def test_check_a_stop_inside_a_word_does_not_split(tmp_path: Path) -> None:
    text = _change_is(" ".join(["a.b"] * 21) + ".")
    _assert_one_finding(tmp_path, text, _line_of(text, "**Change:**"), "over 20 words")


def test_check_a_wrapped_sentence_is_one_sentence(tmp_path: Path) -> None:
    wrapped = " ".join(["word"] * 12) + "\n     " + " ".join(["word"] * 12) + "."
    text = _change_is(wrapped)
    _assert_one_finding(tmp_path, text, _line_of(text, "**Change:**"), "over 20 words")


def test_check_a_stop_at_a_line_end_splits(tmp_path: Path) -> None:
    wrapped = " ".join(["word"] * 12) + ".\n     " + " ".join(["word"] * 12) + "."
    assert _check(tmp_path, _change_is(wrapped)).returncode == 0


@pytest.mark.parametrize("phrase", ["and then", "and also", ", then"])
def test_check_one_action(tmp_path: Path, phrase: str) -> None:
    spaced = phrase if phrase.startswith(",") else f" {phrase}"
    text = _change_is(f"Read the limit{spaced} pass it on.")
    _assert_one_finding(
        tmp_path, text, _line_of(text, "**Change:**"), phrase.strip(", ")
    )


def test_check_one_action_ignores_a_backticked_span(tmp_path: Path) -> None:
    assert (
        _check(tmp_path, _change_is("Run `build and then test` first.")).returncode == 0
    )


@pytest.mark.parametrize("dash", [_EM, _EN])
def test_check_dash_outside_quotes(tmp_path: Path, dash: str) -> None:
    text = _replaced(
        "**Objective:** Add a scan limit.", f"**Objective:** Add {dash} a limit."
    )
    run_lines = _line_of(text, "**Objective:**")
    _assert_one_finding(tmp_path, text, run_lines, "dash")


@pytest.mark.parametrize(
    "line",
    [
        f'**Objective:** Keep "the quote {_EM} intact".',
        f"**Objective:** Keep `the code {_EM} intact`.",
        f"> a blockquote {_EM} line",
    ],
)
def test_check_dash_inside_quotes_is_exempt(tmp_path: Path, line: str) -> None:
    text = _replaced("**Objective:** Add a scan limit.", f"**Objective:** x\n\n{line}")
    assert _check(tmp_path, text).returncode == 0


_VERIFY_LINE = "   - **Verify:** `true` and `true`\n"


def test_check_changes_on_purpose_after_the_last_field(tmp_path: Path) -> None:
    line = '   - **Changes on purpose:** "stay fast" (cited: 1a2b3c4d) - the task asks for it\n'
    text = _replaced(_VERIFY_LINE, _VERIFY_LINE + line)
    run = _check(tmp_path, text)
    assert run.returncode == 0, run.stdout + run.stderr


def test_check_changes_on_purpose_quote_may_hold_a_dash(tmp_path: Path) -> None:
    # The quote runs onto a second line, so only the field exemption covers its dash:
    # read as more Verify text, the dash would be a finding.
    lines = (
        '   - **Changes on purpose:** "stay fast\n'
        f'     {_EM} always" (inferred) - the task asks for it\n'
    )
    text = _replaced(_VERIFY_LINE, _VERIFY_LINE + lines)
    run = _check(tmp_path, text)
    assert run.returncode == 0, run.stdout + run.stderr


def test_check_ignores_fenced_code(tmp_path: Path) -> None:
    text = _change_is(f"Read this.\n     ```\n     ## Steps {_EM}\n     ```")
    assert _check(tmp_path, text).returncode == 0, _check(tmp_path, text).stdout


# ---- verify ---------------------------------------------------------------


def _plan(*verify_lines: str) -> str:
    """A plan whose step N has the N-th Verify line."""
    steps = "".join(
        f"{n}. **Step {n}**\n   - **Change:** Do it.\n   - **Verify:** {line}\n"
        for n, line in enumerate(verify_lines, start=1)
    )
    return f"# Plan: Test\n\n## Steps\n\n{steps}\n## Acceptance\n1. Done - **Check:** `true`\n"


def _verify(
    repo: Path, verify: str, step: int = 1
) -> tuple[subprocess.CompletedProcess[str], Path]:
    (repo / "plans").mkdir(exist_ok=True)
    (repo / "plans" / "My_Plan.md").write_text(_plan(verify))
    run = _tool(repo, "verify", "plans/My_Plan.md", str(step))
    return run, repo / ".whygraph" / "plans" / "my-plan" / "steps" / f"{step}.verify"


def _field(record: str, name: str) -> list[str]:
    return [
        line.split(": ", 1)[1]
        for line in record.splitlines()
        if line.startswith(f"{name}: ")
    ]


def test_verify_pass(repo: Path) -> None:
    run, record_path = _verify(repo, "`printf 'hello\\n'`")

    assert run.returncode == 0, run.stderr
    assert run.stdout == "verify: literal\nhello\n"
    record = record_path.read_text()
    assert record.splitlines() == [
        "command: printf 'hello\\n'",
        "exit: 0",
        f"tree: {_tool(repo, 'snapshot').stdout.strip()}",
        "--- last 40 lines of output",
        "hello",
    ]
    assert _HEX40.match(_field(record, "tree")[0])


def test_verify_fail(repo: Path) -> None:
    run, record_path = _verify(repo, "`echo broken; exit 3`")

    assert run.returncode == 3
    assert run.stdout == "verify: literal\nbroken\n"
    record = record_path.read_text()
    assert _field(record, "exit") == ["3"]
    assert _HEX40.match(_field(record, "tree")[0])
    assert record.endswith("--- last 40 lines of output\nbroken\n")


def test_verify_captures_stderr(repo: Path) -> None:
    run, record_path = _verify(repo, "`echo oops >&2`")
    assert run.stdout == "verify: literal\noops\n"
    assert record_path.read_text().endswith("oops\n")


def test_verify_takes_the_tree_after_the_commands(repo: Path) -> None:
    before = _tool(repo, "snapshot").stdout.strip()
    _, record_path = _verify(repo, "`echo made > generated.txt`")
    tree = _field(record_path.read_text(), "tree")[0]
    assert tree != before
    assert tree == _tool(repo, "snapshot").stdout.strip()
    assert (repo / "generated.txt").is_file()


def test_verify_two_spans_both_run(repo: Path) -> None:
    run, record_path = _verify(repo, "`echo one`, then `echo two`")
    assert run.returncode == 0
    assert run.stdout == "verify: literal\none\ntwo\n"
    assert _field(record_path.read_text(), "command") == ["echo one", "echo two"]


def test_verify_stops_at_the_first_failing_span(repo: Path) -> None:
    run, record_path = _verify(repo, "`exit 7` and `touch second`")

    assert run.returncode == 7
    assert not (repo / "second").exists()
    record = record_path.read_text()
    assert _field(record, "command") == ["exit 7"]
    assert _field(record, "exit") == ["7"]


@pytest.mark.parametrize(
    "line",
    [
        "manual: open the page and `look`",
        "run the tests",
        "`touch ran` returns `x`",
        "",
    ],
)
def test_verify_not_literal(repo: Path, line: str) -> None:
    run, record_path = _verify(repo, line)

    assert run.returncode == 0, run.stderr
    assert run.stdout == "verify: not literal\n"
    assert record_path.read_text() == "exit: not literal\n"
    assert not (repo / "ran").exists()


def test_verify_the_grep_returns_case_is_prose(repo: Path) -> None:
    (repo / "f").write_text("x\n")
    run, record_path = _verify(repo, "`grep x f` returns `x`")
    assert run.returncode == 0
    assert run.stdout.splitlines()[0] == "verify: not literal"
    assert record_path.read_text() == "exit: not literal\n"


def test_verify_a_command_that_exits_65_is_a_failure(repo: Path) -> None:
    run, record_path = _verify(repo, "`exit 65`")

    assert run.returncode == 65
    assert run.stdout.splitlines()[0] == "verify: literal"
    record = record_path.read_text()
    assert _field(record, "exit") == ["65"]
    assert "not literal" not in record


def test_verify_a_step_without_a_verify_line_is_not_literal(repo: Path) -> None:
    (repo / "plans").mkdir()
    (repo / "plans" / "p.md").write_text(
        "# Plan: x\n\n## Steps\n\n1. **Only**\n   - **Change:** y\n"
    )
    run = _tool(repo, "verify", "plans/p.md", "1")
    assert run.returncode == 0
    assert run.stdout == "verify: not literal\n"


def test_verify_finds_the_right_step(repo: Path) -> None:
    (repo / "plans").mkdir()
    lines = [f"`echo step{n}`" for n in range(1, 13)]
    (repo / "plans" / "p.md").write_text(_plan(*lines))

    run = _tool(repo, "verify", "plans/p.md", "1")
    assert run.stdout == "verify: literal\nstep1\n"
    run = _tool(repo, "verify", "plans/p.md", "12")
    assert run.stdout == "verify: literal\nstep12\n"


def test_verify_does_not_leave_the_step_block(repo: Path) -> None:
    (repo / "plans").mkdir()
    text = (
        "## Steps\n\n1. **One**\n   - **Change:** a\n"
        "2. **Two**\n   - **Verify:** `touch ran`\n"
    )
    (repo / "plans" / "p.md").write_text(text)
    run = _tool(repo, "verify", "plans/p.md", "1")
    assert run.stdout == "verify: not literal\n"
    assert not (repo / "ran").exists()


def test_verify_runs_from_the_repo_root(repo: Path) -> None:
    (repo / "sub").mkdir()
    (repo / "plans").mkdir()
    (repo / "plans" / "p.md").write_text(_plan("`test -f a.txt`"))

    run = _tool(repo / "sub", "verify", "../plans/p.md", "1")

    assert run.returncode == 0, run.stdout + run.stderr
    assert (repo / ".whygraph/plans/p/steps/1.verify").is_file()
    assert not (repo / "sub/.whygraph").exists()


def test_verify_keeps_the_last_40_lines(repo: Path) -> None:
    run, record_path = _verify(
        repo, "`awk 'BEGIN { for (i = 1; i <= 50; i++) print i }'`"
    )
    expected = [str(n) for n in range(11, 51)]
    assert run.stdout.splitlines() == ["verify: literal", *expected]
    assert record_path.read_text().splitlines()[4:] == expected


def test_verify_does_not_change_the_callers_locale(repo: Path) -> None:
    # The script forces the C locale for its own text tools only.
    run, _ = _verify(repo, "`echo [$LC_ALL]`")
    assert run.stdout == f"verify: literal\n[{os.environ.get('LC_ALL', '')}]\n"


# ---- verify: the time budget ------------------------------------------------

# Each shell present on the host. With no terminal, dash has no job control.
_SHELLS = {
    name: shell
    for name, shell in (("dash", ("/bin/dash",)), ("bash", ("bash", "--posix")))
    if shutil.which(shell[0])
}
_SHELL_IDS = sorted(_SHELLS)
_FORMS = ("1", "2", "3", "4")
_MARKERS = itertools.count(1)


def _form_available(shell: str, form: str) -> bool:
    """Whether kill form ``form`` works under ``shell`` on this host."""
    if form == "1":
        return shutil.which("setsid") is not None
    if form == "2":
        return shutil.which("perl") is not None
    if form == "3":
        return shell == "bash"
    return True


def _sleeps(marker: str) -> list[str]:
    """The pids of ``sleep <marker>`` processes."""
    found = subprocess.run(
        ["pgrep", "-f", f"^sleep {marker}$"], capture_output=True, text=True
    )
    return found.stdout.split()


@pytest.fixture
def marker() -> Iterator[str]:
    """A sleep interval no other test uses, killed after the test.

    Numeric, because ``sleep`` takes only a number, and built from the pid,
    because xdist workers run in parallel.
    """
    value = f"37{os.getpid()}{next(_MARKERS)}"
    yield value
    subprocess.run(["pkill", "-KILL", "-f", f"^sleep {value}$"], capture_output=True)


def _bounded(
    repo: Path,
    verify: str,
    shell: str,
    *,
    budget: str | None = None,
    form: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    env = {}
    if budget is not None:
        env["WHYGRAPH_VERIFY_TIMEOUT"] = budget
    if form is not None:
        env["WHYGRAPH_KILL_FORM"] = form
    (repo / "plans").mkdir(exist_ok=True)
    (repo / "plans" / "p.md").write_text(_plan(verify))
    run = _tool(repo, "verify", "plans/p.md", "1", shell=_SHELLS[shell], env=env)
    return run, repo / ".whygraph/plans/p/steps/1.verify"


def _forms() -> list[object]:
    return [
        pytest.param(shell, form, id=f"{shell}-form{form}")
        for shell in _SHELL_IDS
        for form in _FORMS
        if _form_available(shell, form)
    ]


@pytest.mark.parametrize(("shell", "form"), _forms())
def test_verify_timeout_kills_the_job(
    repo: Path, marker: str, shell: str, form: str
) -> None:
    run, record_path = _bounded(
        repo, f"`echo started; sleep {marker}`", shell, budget="1", form=form
    )

    assert run.returncode == 124, run.stdout + run.stderr
    assert run.stderr == ""
    assert _sleeps(marker) == []
    record = record_path.read_text().splitlines()
    assert record[0] == f"command: echo started; sleep {marker}"
    assert record[1] == "exit: 124"
    assert _HEX40.match(record[2].removeprefix("tree: "))
    assert record[3:] == [
        "timed out after 1 s",
        "--- last 40 lines of output",
        "started",
    ]


@pytest.mark.parametrize(("shell", "form"), _forms())
def test_verify_timeout_and_a_grandchild(
    repo: Path, marker: str, shell: str, form: str
) -> None:
    run, _ = _bounded(
        repo, f"`(sleep {marker} &); sleep {marker}`", shell, budget="1", form=form
    )

    assert run.returncode == 124, run.stdout + run.stderr
    time.sleep(0.2)
    # Form 4 walks the job's children, so it misses a grandchild whose
    # parent has already exited. The other forms kill the whole group.
    assert len(_sleeps(marker)) == (1 if form == "4" else 0)


@pytest.mark.parametrize(("shell", "form"), _forms())
def test_verify_term_kills_the_job_and_exits_143(
    repo: Path, marker: str, shell: str, form: str
) -> None:
    (repo / "plans").mkdir()
    (repo / "plans" / "p.md").write_text(_plan(f"`sleep {marker}`"))
    proc = subprocess.Popen(
        [*_SHELLS[shell], str(_SCRIPT), "verify", "plans/p.md", "1"],
        cwd=repo,
        env={**_env(), "WHYGRAPH_KILL_FORM": form},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not _sleeps(marker) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _sleeps(marker)
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=20)
    finally:
        proc.kill()

    assert proc.returncode == 143, err
    time.sleep(0.2)
    assert _sleeps(marker) == []


@pytest.mark.parametrize("shell", _SHELL_IDS)
def test_verify_fast_command_returns_at_once(repo: Path, shell: str) -> None:
    started = time.monotonic()
    run, record_path = _bounded(repo, "`true`", shell)
    assert run.returncode == 0, run.stderr
    # Far below the 540 s default budget: the watchdog does not hold the call.
    # Not tighter, so a loaded xdist worker does not fail it.
    assert time.monotonic() - started < 30
    assert "timed out" not in record_path.read_text()


@pytest.mark.parametrize("shell", _SHELL_IDS)
def test_verify_failing_span_keeps_its_exit_code(repo: Path, shell: str) -> None:
    run, record_path = _bounded(repo, "`exit 3`", shell, budget="5")
    assert run.returncode == 3
    record = record_path.read_text()
    assert _field(record, "exit") == ["3"]
    assert "timed out" not in record


@pytest.mark.parametrize("shell", _SHELL_IDS)
def test_verify_own_exit_124_is_not_a_timeout(repo: Path, shell: str) -> None:
    run, record_path = _bounded(repo, "`exit 124`", shell, budget="5")
    assert run.returncode == 124
    assert "timed out" not in record_path.read_text()


@pytest.mark.parametrize("shell", _SHELL_IDS)
def test_verify_spans_share_one_budget(repo: Path, shell: str) -> None:
    # Each span fits the budget alone, both together do not. The margin keeps
    # the second span started on a loaded xdist worker.
    run, record_path = _bounded(repo, "`sleep 2` and `sleep 2`", shell, budget="4")
    assert run.returncode == 124
    record = record_path.read_text()
    assert _field(record, "command") == ["sleep 2", "sleep 2"]
    assert _field(record, "exit") == ["124"]


@pytest.mark.parametrize("shell", _SHELL_IDS)
@pytest.mark.parametrize("budget", ["0", "09", "x", "-1"])
def test_verify_bad_budget_exits_64(repo: Path, shell: str, budget: str) -> None:
    run, record_path = _bounded(repo, "`touch ran`", shell, budget=budget)
    assert run.returncode == 64
    assert run.stdout == ""
    assert "WHYGRAPH_VERIFY_TIMEOUT" in run.stderr
    assert not (repo / "ran").exists()
    assert not record_path.exists()


@pytest.mark.parametrize("shell", _SHELL_IDS)
def test_verify_budget_over_580_is_clamped(repo: Path, shell: str) -> None:
    run, record_path = _bounded(repo, "`printf 'hello\\n'`", shell, budget="9999")
    assert run.returncode == 0
    assert run.stderr == "plan-tool.sh: WHYGRAPH_VERIFY_TIMEOUT clamped to 580 s\n"
    assert run.stdout == "verify: literal\nhello\n"
    assert len(record_path.read_text().splitlines()) == 5


@pytest.mark.parametrize(
    ("shell", "form"),
    [
        pytest.param(shell, form, id=f"{shell}-form{form}")
        for shell in _SHELL_IDS
        for form in (*_FORMS, "5", "x")
        if form not in _FORMS or not _form_available(shell, form)
    ],
)
def test_verify_unavailable_kill_form_exits_64(
    repo: Path, shell: str, form: str
) -> None:
    run, record_path = _bounded(repo, "`touch ran`", shell, form=form)
    assert run.returncode == 64
    assert run.stdout == ""
    assert not (repo / "ran").exists()
    assert not record_path.exists()


# ---- begin, end, pass, next, changed ------------------------------------------

_STEPS = Path(".whygraph/plans/p/steps")
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


@pytest.fixture(params=_SHELL_IDS)
def posix(request: pytest.FixtureRequest) -> str:
    """Each shell present on the host, as a test id."""
    return request.param


def _steps_plan(*steps: tuple[str, str], log: str = "") -> str:
    """A plan whose step N has the N-th ``(Files, Verify)`` pair."""
    body = "".join(
        f"{n}. **Step {n}**\n   - **Files:** {files}\n   - **Verify:** {verify}\n"
        for n, (files, verify) in enumerate(steps, start=1)
    )
    text = f"# Plan: Test\n\n## Steps\n\n{body}\n## Acceptance\n1. Done - **Check:** `true`\n"
    return text + (f"\n## Implementation log\n\n{log}" if log else "")


def _write_plan(repo: Path, text: str) -> None:
    (repo / "plans").mkdir(exist_ok=True)
    (repo / "plans" / "p.md").write_text(text)


def _run(
    repo: Path, posix: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return _tool(repo, *args, shell=_SHELLS[posix], env=env)


def _begin(repo: Path, posix: str, step: str = "1") -> str:
    run = _run(repo, posix, "begin", "plans/p.md", step)
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


def _end(
    repo: Path,
    posix: str,
    t0: str,
    *extra: str,
    step: str = "1",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run(repo, posix, "end", "plans/p.md", step, t0, *extra, env=env)


def _snapshot(repo: Path) -> str:
    return _tool(repo, "snapshot").stdout.strip()


def _steps_file(repo: Path, name: str, text: str) -> Path:
    path = repo / _STEPS / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_begin_writes_the_start_tree(repo: Path, posix: str) -> None:
    _write_plan(repo, _steps_plan(("a.txt", "`true`")))
    t0 = _begin(repo, posix)
    assert t0 == _snapshot(repo)
    assert (repo / _STEPS / "1.t0").read_text() == f"{t0}\n"
    assert (repo / _STEPS / "attempts").read_text() == f"1 begin {t0}\n"


def test_begin_keeps_the_start_tree_after_a_begin_line(repo: Path, posix: str) -> None:
    _write_plan(repo, _steps_plan(("a.txt", "`true`")))
    t0 = _begin(repo, posix)
    (repo / "a.txt").write_text("changed\n")
    assert _begin(repo, posix) == t0
    assert (repo / _STEPS / "attempts").read_text() == f"1 begin {t0}\n" * 2


def test_begin_replaces_a_start_tree_without_a_begin_line(
    repo: Path, posix: str
) -> None:
    _write_plan(repo, _steps_plan(("a.txt", "`true`")))
    start = _steps_file(repo, "1.t0", f"{_EMPTY_TREE}\n")
    t0 = _begin(repo, posix)
    assert t0 == _snapshot(repo) != _EMPTY_TREE
    assert start.read_text() == f"{t0}\n"


def test_begin_replaces_an_invalid_start_tree(repo: Path, posix: str) -> None:
    _write_plan(repo, _steps_plan(("a.txt", "`true`")))
    _steps_file(repo, "attempts", "1 begin x\n")
    start = _steps_file(repo, "1.t0", "garbage\n")
    t0 = _begin(repo, posix)
    assert t0 == _snapshot(repo)
    assert start.read_text() == f"{t0}\n"


def test_begin_retakes_when_a_later_step_ran(repo: Path, posix: str) -> None:
    _write_plan(repo, _steps_plan(("a.txt", "`true`"), ("a.txt", "`true`")))
    first = _begin(repo, posix)
    (repo / "a.txt").write_text("changed\n")
    for name in ("2.t0", "3.t0", "12.t0"):
        _steps_file(repo, name, f"{first}\n")
    t0 = _begin(repo, posix)
    assert t0 == _snapshot(repo) != first
    assert (repo / _STEPS / "1.t0").read_text() == f"{t0}\n"
    assert sorted(p.name for p in (repo / _STEPS).glob("*.t0")) == ["1.t0"]


def test_begin_deletes_the_done_marker(repo: Path, posix: str) -> None:
    _write_plan(repo, _steps_plan(("a.txt", "`true`")))
    done = _steps_file(repo, "1.done", f"{_EMPTY_TREE}\n")
    _begin(repo, posix)
    assert not done.exists()


def test_end_always_runs_verify(repo: Path, posix: str) -> None:
    _write_plan(repo, _steps_plan(("ran", "`touch ran`")))
    t0 = _begin(repo, posix)
    tree = _snapshot(repo)
    _steps_file(repo, "1.verify", f"command: touch ran\nexit: 0\ntree: {tree}\n")
    run = _end(repo, posix, t0)
    assert run.returncode == 0, run.stderr
    assert (repo / "ran").is_file()


def test_end_writes_the_record_trees_and_diff(repo: Path, posix: str) -> None:
    # A new size: the snapshot index is a copy, so it misses a racy same-size edit.
    (repo / "a.txt").write_text("dirty, before the step\n")
    (repo / "junk.txt").write_text("untracked\n")
    _write_plan(repo, _steps_plan(("b.txt (new)", "`echo new > b.txt`")))
    t0 = _begin(repo, posix)
    run = _end(repo, posix, t0)

    assert run.returncode == 0, run.stderr
    t1 = _snapshot(repo)
    assert run.stdout == f"end: {t0} {t1} exit 0\n"
    record = (repo / _STEPS / "1.verify").read_text()
    assert _field(record, "command") == ["echo new > b.txt"]
    assert _field(record, "tree") == [t1]
    assert (repo / _STEPS / "1.t1").read_text() == f"{t1}\n"
    diff = (repo / _STEPS / "1.diff").read_text()
    assert diff == _git(repo, "diff", "-U3", "--no-color", t0, t1) + "\n"
    assert re.findall(r"^diff --git a/(\S+)", diff, re.M) == ["b.txt"]


def test_end_uses_the_t0_argument(repo: Path, posix: str) -> None:
    _write_plan(repo, _steps_plan(("b.txt", "`echo new > b.txt`")))
    t0 = _begin(repo, posix)
    (repo / _STEPS / "1.t0").write_text(f"{_EMPTY_TREE}\n")
    run = _end(repo, posix, t0)
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith(f"end: {t0} ")
    diff = (repo / _STEPS / "1.diff").read_text()
    assert re.findall(r"^diff --git a/(\S+)", diff, re.M) == ["b.txt"]


def test_end_ignores_an_external_diff(repo: Path, posix: str) -> None:
    script = repo.parent / "ext.sh"
    script.write_text(f"#!/bin/sh\ntouch {repo.parent}/ext-ran\necho EXTERNAL\n")
    script.chmod(0o755)
    _git(repo, "config", "diff.external", str(script))
    _git(repo, "config", "diff.txt.textconv", str(script))
    (repo / ".gitattributes").write_text("*.txt diff=txt\n")
    _write_plan(repo, _steps_plan(("b.txt", "`echo new > b.txt`")))
    t0 = _begin(repo, posix)
    assert _end(repo, posix, t0).returncode == 0
    diff = (repo / _STEPS / "1.diff").read_text()
    assert "+new" in diff
    assert "EXTERNAL" not in diff
    assert not (repo.parent / "ext-ran").exists()


def test_end_lists_paths_outside_files(repo: Path, posix: str) -> None:
    files = "`b.txt`:1-2 (new), dir/, ./c.py:3 (see `x`, y), sub"
    verify = (
        "`mkdir -p dir/deep sub dirx && touch b.txt dir/deep/y c.py sub/z dirx/z d.txt`"
    )
    _write_plan(repo, _steps_plan((files, verify)))
    t0 = _begin(repo, posix)
    run = _end(repo, posix, t0)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines()[1:] == [
        "outside Files: d.txt",
        "outside Files: dirx/z",
        "outside Files: sub/z",
    ]


@pytest.mark.parametrize(
    ("verify", "code", "expected"),
    [
        ("`nvm use`", "127", 4),
        ("`true && LANG=C nvm use`", "127", 4),
        ("`sh scripts/nonexist.sh`", _SH_MISSING, 1),
        ("`exit 124`", "124", 1),
        ("`exit 126`", "126", 1),
        ("`exit 3`", "3", 1),
        ("`./bin/new-check`", "127", 1),
        ("`$NOPE x`", "127", 4),
    ],
)
def test_end_exit_codes(
    repo: Path, posix: str, verify: str, code: str, expected: int
) -> None:
    _write_plan(repo, _steps_plan(("bin/new-check (new)", verify)))
    t0 = _begin(repo, posix)
    run = _end(repo, posix, t0)
    assert run.returncode == expected, run.stdout + run.stderr
    assert run.stdout.splitlines()[0].endswith(f" exit {code}")
    record = (repo / _STEPS / "1.verify").read_text()
    assert _field(record, "exit") == [code]
    assert "timed out" not in record


@pytest.mark.parametrize(
    "folder",
    [
        ".claude/skills/whygraph-plan",
        ".cursor/skills/whygraph-plan",
        ".github/whygraph",
        ".codex/whygraph",
    ],
)
@pytest.mark.parametrize("tail", ["true", "false", "nvm use"])
def test_end_a_plan_tool_change_exits_65(
    repo: Path, posix: str, folder: str, tail: str
) -> None:
    verify = f"`mkdir -p {folder} && echo x > {folder}/plan-tool.sh && {tail}`"
    _write_plan(repo, _steps_plan((f"{folder}/plan-tool.sh", verify)))
    t0 = _begin(repo, posix)
    run = _end(repo, posix, t0)
    assert run.returncode == 65, run.stdout + run.stderr
    assert "plan tool" in run.stderr
    assert run.stdout.startswith(f"end: {t0} ")


def test_end_timeout_exits_124(repo: Path, posix: str, marker: str) -> None:
    _write_plan(repo, _steps_plan(("a.txt", f"`sleep {marker}`")))
    t0 = _begin(repo, posix)
    run = _end(repo, posix, t0, env={"WHYGRAPH_VERIFY_TIMEOUT": "1"})
    assert run.returncode == 124, run.stdout + run.stderr
    assert _sleeps(marker) == []
    record = (repo / _STEPS / "1.verify").read_text()
    assert _field(record, "exit") == ["124"]
    assert "timed out after 1 s" in record.splitlines()


def test_end_does_not_run_verify_without_a_valid_t0(repo: Path, posix: str) -> None:
    _write_plan(repo, _steps_plan(("ran", "`touch ran`")))
    for t0 in ("x", "a" * 40, _EMPTY_TREE[:-1]):
        run = _end(repo, posix, t0)
        assert run.returncode == 64, run.stdout + run.stderr
        assert run.stdout == ""
    assert not (repo / "ran").exists()


def test_end_not_literal_takes_its_own_end_tree(repo: Path, posix: str) -> None:
    _write_plan(repo, _steps_plan(("b.txt", "manual: look at `b.txt`")))
    t0 = _begin(repo, posix)
    (repo / "b.txt").write_text("new\n")
    run = _end(repo, posix, t0)

    assert run.returncode == 0, run.stderr
    t1 = _snapshot(repo)
    assert t1 != t0
    assert run.stdout == f"end: {t0} {t1} exit not literal\n"
    assert (repo / _STEPS / "1.verify").read_text() == "exit: not literal\n"
    assert (repo / _STEPS / "1.t1").read_text() == f"{t1}\n"
    assert "b.txt" in (repo / _STEPS / "1.diff").read_text()

    assert _run(repo, posix, "pass", "plans/p.md", "1").returncode == 0
    assert (repo / _STEPS / "1.done").read_text() == f"{t1}\n"
    assert _run(repo, posix, "next", "plans/p.md").stdout == "done\n"


def test_end_deletes_the_done_marker(repo: Path, posix: str) -> None:
    _write_plan(repo, _steps_plan(("a.txt", "`true`")))
    t0 = _begin(repo, posix)
    done = _steps_file(repo, "1.done", f"{t0}\n")
    assert _end(repo, posix, t0).returncode == 0
    assert not done.exists()


def test_end_reads_the_plan_from_a_blob(repo: Path, posix: str) -> None:
    saved = _steps_plan(("right", "`touch right`"))
    blob = _git(repo, "hash-object", "-w", "--stdin", stdin=saved)
    _write_plan(repo, _steps_plan(("wrong", "`touch wrong`")))
    t0 = _begin(repo, posix)
    run = _end(repo, posix, t0, blob)
    assert run.returncode == 0, run.stderr
    assert (repo / "right").exists()
    assert not (repo / "wrong").exists()


def test_end_a_blob_that_cannot_be_read_exits_65(repo: Path, posix: str) -> None:
    _write_plan(repo, _steps_plan(("ran", "`touch ran`")))
    t0 = _begin(repo, posix)
    run = _end(repo, posix, t0, "c" * 40)
    assert run.returncode == 65
    assert "the saved plan was changed" in run.stderr
    assert not (repo / "ran").exists()


@pytest.mark.parametrize(("shell", "form"), _forms())
def test_end_timeout_and_a_grandchild(
    repo: Path, marker: str, shell: str, form: str
) -> None:
    _write_plan(repo, _steps_plan(("a.txt", f"`(sleep {marker} &); sleep {marker}`")))
    t0 = _begin(repo, shell)
    run = _end(
        repo,
        shell,
        t0,
        env={"WHYGRAPH_VERIFY_TIMEOUT": "1", "WHYGRAPH_KILL_FORM": form},
    )
    assert run.returncode == 124, run.stdout + run.stderr
    time.sleep(0.2)
    assert len(_sleeps(marker)) == (1 if form == "4" else 0)


@pytest.mark.parametrize(("shell", "form"), _forms())
def test_end_term_kills_the_job_and_exits_143(
    repo: Path, marker: str, shell: str, form: str
) -> None:
    _write_plan(repo, _steps_plan(("a.txt", f"`sleep {marker}`")))
    t0 = _begin(repo, shell)
    proc = subprocess.Popen(
        [*_SHELLS[shell], str(_SCRIPT), "end", "plans/p.md", "1", t0],
        cwd=repo,
        env={**_env(), "WHYGRAPH_KILL_FORM": form},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not _sleeps(marker) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _sleeps(marker)
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=20)
    finally:
        proc.kill()

    assert proc.returncode == 143, err
    time.sleep(0.2)
    assert _sleeps(marker) == []


def test_pass_needs_an_end_tree(repo: Path, posix: str) -> None:
    _write_plan(repo, _steps_plan(("a.txt", "`true`")))
    _steps_file(repo, "1.t1", "garbage\n")
    run = _run(repo, posix, "pass", "plans/p.md", "1")
    assert run.returncode == 64
    assert not (repo / _STEPS / "1.done").exists()


def test_next_skips_the_steps_marked_done(repo: Path, posix: str) -> None:
    _write_plan(repo, _steps_plan(*[("a.txt", "`true`")] * 3))

    def next_step() -> str:
        run = _run(repo, posix, "next", "plans/p.md")
        assert run.returncode == 0, run.stderr
        return run.stdout

    assert next_step() == "1\n"
    _steps_file(repo, "1.done", f"{_EMPTY_TREE}\n")
    _steps_file(repo, "3.done", f"{_EMPTY_TREE}\n")
    assert next_step() == "2\n"
    _steps_file(repo, "2.done", f"{_EMPTY_TREE}\n")
    assert next_step() == "done\n"


def test_next_returns_a_passing_step_without_a_done_marker(
    repo: Path, posix: str
) -> None:
    _write_plan(repo, _steps_plan(("a.txt", "`true`")))
    t0 = _begin(repo, posix)
    assert _end(repo, posix, t0).returncode == 0
    assert _run(repo, posix, "next", "plans/p.md").stdout == "1\n"


_LOG = "- [x] Step 1: Step 1 (verified at 2026-11-12T00:00:00Z)\n"


def _logged_step(repo: Path, posix: str, log: str = _LOG) -> None:
    """Step 1 passed under an older harness: a log line, a record and an end tree."""
    _write_plan(repo, _steps_plan(("a.txt", "`true`"), ("a.txt", "`true`"), log=log))
    run = _run(repo, posix, "verify", "plans/p.md", "1")
    assert run.returncode == 0, run.stderr
    tree = _field((repo / _STEPS / "1.verify").read_text(), "tree")[0]
    _steps_file(repo, "1.t1", f"{tree}\n")


def test_next_counts_a_logged_step_with_a_passing_record(
    repo: Path, posix: str
) -> None:
    _logged_step(repo, posix)
    assert _run(repo, posix, "next", "plans/p.md").stdout == "2\n"


@pytest.mark.parametrize(
    "fault",
    [
        "begin line",
        "no record",
        "failed record",
        "other tree",
        "no log line",
        "log line in a fence",
    ],
)
def test_next_returns_a_logged_step_without_proof(
    repo: Path, posix: str, fault: str
) -> None:
    log = _LOG
    if fault == "no log line":
        log = "- [ ] Step 1: Step 1 (failed)\n"
    elif fault == "log line in a fence":
        log = f"```\n{_LOG}```\n"
    _logged_step(repo, posix, log)
    if fault == "begin line":
        _steps_file(repo, "attempts", "1 begin x\n")
    elif fault == "no record":
        (repo / _STEPS / "1.verify").unlink()
    elif fault == "failed record":
        record = repo / _STEPS / "1.verify"
        record.write_text(record.read_text().replace("exit: 0", "exit: 1"))
    elif fault == "other tree":
        _steps_file(repo, "1.t1", f"{_EMPTY_TREE}\n")
    assert _run(repo, posix, "next", "plans/p.md").stdout == "1\n"


def test_changed(repo: Path, posix: str) -> None:
    before = _snapshot(repo)
    run = _run(repo, posix, "changed", before, before)
    assert (run.returncode, run.stdout) == (0, "")
    (repo / ".coverage").write_text("data\n")
    run = _run(repo, posix, "changed", before, _snapshot(repo))
    assert (run.returncode, run.stdout) == (1, ".coverage\n")


_DECOYS = """\
# Plan: Test

5. **Above the section**
   - **Verify:** `touch above`

```
## Steps
6. **Fenced**
   - **Verify:** `touch ran`
```

"""


def test_parser_reads_only_the_steps_section(repo: Path, posix: str) -> None:
    _write_plan(repo, _DECOYS + _steps_plan(("a.txt", "`true`")).split("\n\n", 1)[1])
    for step in ("5", "6"):
        for command in ("verify", "begin", "baseline"):
            run = _run(repo, posix, command, "plans/p.md", step)
            assert run.returncode == 64, run.stdout
    run = _run(repo, posix, "probe", "plans/p.md")
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == "span: step 1: true\nspan: acceptance 1: true\n"
    run = _run(repo, posix, "baseline", "plans/p.md", "1", "first")
    assert run.returncode == 0, run.stdout + run.stderr
    run = _run(repo, posix, "verify", "plans/p.md", "1")
    assert run.returncode == 0, run.stderr
    assert not (repo / "above").exists()
    assert not (repo / "ran").exists()
    assert _run(repo, posix, "next", "plans/p.md").stdout == "1\n"
    _steps_file(repo, "1.done", f"{_EMPTY_TREE}\n")
    assert _run(repo, posix, "next", "plans/p.md").stdout == "done\n"


def test_parser_skips_a_fenced_verify_inside_a_step(repo: Path, posix: str) -> None:
    text = _steps_plan(("a.txt", "`touch real`")).replace(
        "   - **Verify:**",
        "   ```\n   - **Verify:** `touch ran`\n   ```\n   - **Verify:**",
    )
    _write_plan(repo, text)
    assert _run(repo, posix, "verify", "plans/p.md", "1").returncode == 0
    assert (repo / "real").exists()
    assert not (repo / "ran").exists()


def test_next_with_no_steps_exits_64(repo: Path, posix: str) -> None:
    _write_plan(repo, "# Plan: Test\n\n1. **Not a step**\n")
    assert _run(repo, posix, "next", "plans/p.md").returncode == 64


def test_snapshot_and_end_ignore_fsmonitor(repo: Path, posix: str) -> None:
    hook = repo.parent / "fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch {repo.parent}/fsmonitor-ran\nexit 1\n")
    hook.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", str(hook))
    _write_plan(repo, _steps_plan(("a.txt", "`true`")))
    assert _run(repo, posix, "snapshot").returncode == 0
    t0 = _begin(repo, posix)
    assert _end(repo, posix, t0).returncode == 0
    assert _run(repo, posix, "changed", t0, t0).returncode == 0
    assert not (repo.parent / "fsmonitor-ran").exists()


# ---- probe, baseline, accept ---------------------------------------------------


def _full_plan(
    *steps: tuple[str, str, str | None], checks: tuple[str, ...] = ("`true`",)
) -> str:
    """A plan whose step N has the N-th ``(Files, Verify, Test first)``.

    A Test first of ``None`` leaves the line out. Item i of ``checks`` is
    Acceptance item i's Check text.
    """
    body = ""
    for n, (files, verify, test_first) in enumerate(steps, start=1):
        body += f"{n}. **Step {n}**\n   - **Files:** {files}\n"
        if test_first is not None:
            body += f"   - **Test first:** {test_first}\n"
        body += f"   - **Verify:** {verify}\n"
    items = "".join(
        f"{i}. Item {i} - **Check:** {check}\n"
        for i, check in enumerate(checks, start=1)
    )
    return f"# Plan: Test\n\n## Steps\n\n{body}\n## Acceptance\n{items}"


def _probe(
    repo: Path, posix: str, text: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    _write_plan(repo, text)
    return _run(repo, posix, "probe", "plans/p.md", env=env)


def test_probe_lists_every_miss(repo: Path, posix: str) -> None:
    text = _full_plan(
        ("a.txt", "`nvm use && true`", "none"),
        ("a.txt", "`nvm use`", "`tests/t.py::test_a`"),
        checks=("`nvm use && true`", "manual: open `nvm use`"),
    )
    run = _probe(repo, posix, text)
    assert run.returncode == 4, run.stdout + run.stderr
    assert run.stdout.splitlines() == [
        "span: step 1: nvm use && true",
        "span: step 2: nvm use",
        "span: acceptance 1: nvm use && true",
        "missing: nvm (step 1)",
        "missing: nvm (step 2)",
        "missing: nvm (acceptance 1)",
    ]


@pytest.mark.parametrize(
    "span",
    [
        "true",
        'python3 -c "import x; assert x"',
        "grep -E 'a|b' f",
        "LANG=C uv run pytest",
        "(cd sub && make)",
        "{ true; } && ! false",
        "awk 'BEGIN { for (i = 1; i <= 50; i++) print i }'",
        "sh -c 'exit 0'",
        "cd sub && sh scripts/elsewhere.sh",
    ],
)
def test_probe_passes_present_commands(
    repo: Path, posix: str, tmp_path: Path, span: str
) -> None:
    # A stand-in uv: the test needs a command on PATH, not uv itself.
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "uv").write_text("#!/bin/sh\nexit 0\n")
    (tools / "uv").chmod(0o755)
    path = f"{tools}{os.pathsep}{os.environ.get('PATH', '')}"
    text = _full_plan(("a.txt", f"`{span}`", "none"), checks=(f"`{span}`",))
    run = _probe(repo, posix, text, env={"PATH": path})
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == f"span: step 1: {span}\nspan: acceptance 1: {span}\n"


@pytest.mark.parametrize("named", [True, False])
def test_probe_skips_the_paths_a_step_creates(
    repo: Path, posix: str, named: bool
) -> None:
    files = "scripts/new.sh (new), `bin/new-check` (new)" if named else "a.txt"
    text = _full_plan(
        (files, "`touch a.txt`", "none"),
        ("a.txt", "`sh scripts/new.sh`", "none"),
        checks=("`./bin/new-check`",),
    )
    run = _probe(repo, posix, text)
    misses = [line for line in run.stdout.splitlines() if line.startswith("missing:")]
    if named:
        assert (run.returncode, misses) == (0, [])
    else:
        assert run.returncode == 4
        assert misses == [
            "missing: scripts/new.sh (step 2)",
            "missing: ./bin/new-check (acceptance 1)",
        ]


def test_probe_runs_no_command(repo: Path, posix: str) -> None:
    text = _full_plan(
        ("a.txt", "`touch marker`", "none"),
        ("a.txt", "`$(touch marker) x`", "none"),
        checks=("`touch marker`",),
    )
    run = _probe(repo, posix, text)
    assert run.returncode == 4, run.stdout + run.stderr
    assert run.stdout.splitlines()[-1] == (
        "missing: $(touch (step 2) (not a plain command)"
    )
    assert not (repo / "marker").exists()


def _baseline(
    repo: Path, posix: str, step: str, *extra: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return _run(repo, posix, "baseline", "plans/p.md", step, *extra, env=env)


def _baseline_lines(repo: Path) -> list[str]:
    return (repo / _STEPS / "baseline").read_text().splitlines()


def _result(line: str) -> str:
    """The result of a baseline line ``step <N> <hash>: <result>``."""
    head, result = line.split(": ", 1)
    kind, _, key = head.split(" ")
    assert kind == "step"
    assert _HEX40.match(key)
    return result


def test_baseline_first_starts_a_pass(repo: Path, posix: str) -> None:
    _write_plan(repo, _full_plan(("a.txt", "`true`", "none")))
    _steps_file(repo, "baseline", "step 9 x: pass\n")
    run = _baseline(repo, posix, "1", "first")
    assert run.returncode == 0, run.stderr
    lines = _baseline_lines(repo)
    assert len(lines) == 1
    assert run.stdout == f"{lines[0]}\n"
    assert lines[0].startswith("step 1 ")
    assert _result(lines[0]) == "pass"
    assert not (repo / _STEPS / "1.verify").exists()


def test_baseline_runs_each_verify_once_per_pass(repo: Path, posix: str) -> None:
    same = "`echo x >> runs.txt`"
    _write_plan(
        repo,
        _full_plan(
            ("a.txt", same, "none"),
            ("a.txt", same, "none"),
            ("a.txt", "`echo y >> runs.txt`", "none"),
        ),
    )
    for step, extra in (("1", ("first",)), ("2", ()), ("3", ())):
        assert _baseline(repo, posix, step, *extra).returncode == 0
    assert (repo / "runs.txt").read_text() == "x\ny\n"
    keys = [line.split(":")[0].split(" ")[2] for line in _baseline_lines(repo)]
    assert keys[0] == keys[1] != keys[2]

    # A new pass never copies a line of the last one.
    assert _baseline(repo, posix, "2", "first").returncode == 0
    assert (repo / "runs.txt").read_text() == "x\ny\nx\n"
    assert len(_baseline_lines(repo)) == 1
    assert not list((repo / _STEPS).glob("*.verify"))


def test_baseline_a_changed_verify_gets_a_new_key(repo: Path, posix: str) -> None:
    _write_plan(repo, _full_plan(("a.txt", "`echo x >> runs.txt`", "none")))
    assert _baseline(repo, posix, "1", "first").returncode == 0
    _write_plan(repo, _full_plan(("a.txt", "`echo  x >> runs.txt`", "none")))
    assert _baseline(repo, posix, "1").returncode == 0
    first, second = (line.split(":")[0] for line in _baseline_lines(repo))
    assert first != second
    assert (repo / "runs.txt").read_text() == "x\nx\n"


@pytest.mark.parametrize(
    ("test_first", "runs"),
    [
        ("`tests/t.py::test_a`", False),
        ("tests/t.py::test_new - added first", False),
        ("none", True),
        ("None - no new test", True),
        ('"none"', True),
        ("`none`.", True),
        (None, True),
    ],
)
def test_baseline_test_first(
    repo: Path, posix: str, test_first: str | None, runs: bool
) -> None:
    _write_plan(repo, _full_plan(("a.txt", "`touch ran`", test_first)))
    run = _baseline(repo, posix, "1", "first")
    assert run.returncode == 0, run.stderr
    assert (repo / "ran").exists() == runs
    assert _result(_baseline_lines(repo)[0]) == ("pass" if runs else "test first")


def test_baseline_a_test_first_line_is_never_copied(repo: Path, posix: str) -> None:
    verify = "`echo x >> runs.txt`"
    _write_plan(
        repo,
        _full_plan(("a.txt", verify, "`t.py::test_a`"), ("a.txt", verify, "none")),
    )
    assert _baseline(repo, posix, "1", "first").returncode == 0
    assert _baseline(repo, posix, "2").returncode == 0
    assert [_result(line) for line in _baseline_lines(repo)] == ["test first", "pass"]
    assert (repo / "runs.txt").read_text() == "x\n"


@pytest.mark.parametrize(
    ("verify", "code", "result"),
    [
        ("`nvm use`", 4, "cannot run 127: nvm use"),
        ("`true && LANG=C nvm use`", 4, "cannot run 127: true && LANG=C nvm use"),
        ("`sh scripts/nonexist.sh`", 0, f"fail {_SH_MISSING}: "),
        ("`./bin/new-check`", 0, "fail 127: "),
        ("`echo broken; exit 3`", 0, "fail 3: broken"),
        ("`exit 124`", 0, "fail 124: "),
        ("manual: look at `x`", 0, "not literal"),
    ],
)
def test_baseline_results(
    repo: Path, posix: str, verify: str, code: int, result: str
) -> None:
    _write_plan(repo, _full_plan(("bin/new-check (new)", verify, "none")))
    run = _baseline(repo, posix, "1", "first")
    assert run.returncode == code, run.stdout + run.stderr
    found = _result(_baseline_lines(repo)[0])
    assert found.startswith(result) if result.endswith(": ") else found == result
    assert not (repo / _STEPS / "1.verify").exists()


def test_baseline_excerpt_is_one_short_line(repo: Path, posix: str) -> None:
    verify = "`printf 'first\\nlast\\tline %0300d\\n\\n  \\n' 0; exit 2`"
    _write_plan(repo, _full_plan(("a.txt", verify, "none")))
    assert _baseline(repo, posix, "1", "first").returncode == 0
    lines = _baseline_lines(repo)
    assert len(lines) == 1
    excerpt = _result(lines[0]).removeprefix("fail 2: ")
    assert excerpt == ("last line " + "0" * 300)[:200]


def test_baseline_timeout_exits_3(repo: Path, posix: str, marker: str) -> None:
    _write_plan(repo, _full_plan(("a.txt", f"`sleep {marker}`", "none")))
    run = _baseline(repo, posix, "1", "first", env={"WHYGRAPH_VERIFY_TIMEOUT": "1"})
    assert run.returncode == 3, run.stdout + run.stderr
    assert _sleeps(marker) == []
    assert _result(_baseline_lines(repo)[0]) == f"timeout 1: sleep {marker}"
    assert not (repo / _STEPS / "1.verify").exists()


def test_baseline_a_new_pass_runs_again_after_a_timeout(repo: Path, posix: str) -> None:
    _write_plan(repo, _full_plan(("a.txt", "`sleep 1.5 && touch finished`", "none")))
    run = _baseline(repo, posix, "1", "first", env={"WHYGRAPH_VERIFY_TIMEOUT": "1"})
    assert run.returncode == 3, run.stdout + run.stderr
    assert not (repo / "finished").exists()
    run = _baseline(repo, posix, "1", "first", env={"WHYGRAPH_VERIFY_TIMEOUT": "10"})
    assert run.returncode == 0, run.stdout + run.stderr
    assert (repo / "finished").exists()
    assert [_result(line) for line in _baseline_lines(repo)] == ["pass"]


def test_baseline_reads_the_plan_from_a_blob(repo: Path, posix: str) -> None:
    saved = _full_plan(("right", "`touch right`", "none"))
    blob = _git(repo, "hash-object", "-w", "--stdin", stdin=saved)
    _write_plan(repo, _full_plan(("wrong", "`touch wrong`", "none")))
    run = _baseline(repo, posix, "1", "first", blob)
    assert run.returncode == 0, run.stderr
    assert (repo / "right").exists()
    assert not (repo / "wrong").exists()
    assert _baseline(repo, posix, "1", "c" * 40).returncode == 65


@pytest.mark.parametrize(
    "output",
    [
        "collected 0 items",
        "No tests ran",
        "===== no tests ran in 0.01s =====",
        "  0 passing (2ms)",
        "running 0 tests",
        "No tests to run",
        "no tests found",
        "Ran 0 tests in 0.000s",
        "COLLECTED 0 ITEMS",
    ],
)
def test_baseline_a_pass_that_ran_no_test(repo: Path, posix: str, output: str) -> None:
    """Plan 54 section 5.13: the first word stays ``pass``."""
    _write_plan(repo, _full_plan(("a.txt", f'`echo "{output}"`', "none")))
    run = _baseline(repo, posix, "1", "first")
    assert run.returncode == 0, run.stderr
    assert _result(_baseline_lines(repo)[0]) == "pass (no test ran)"
    assert run.stdout.rstrip("\n").endswith(": pass (no test ran)")


@pytest.mark.parametrize(
    "output",
    [
        "collected 10 items",
        "10 passing (2ms)",
        "running 10 tests",
        "Ran 20 tests in 0.1s",
        "100 tests passed",
        "0 testsuites",
        "a0 tests",
        "ok",
    ],
)
def test_baseline_a_pass_that_ran_a_test_stays_pass(
    repo: Path, posix: str, output: str
) -> None:
    _write_plan(repo, _full_plan(("a.txt", f'`echo "{output}"`', "none")))
    assert _baseline(repo, posix, "1", "first").returncode == 0
    assert _result(_baseline_lines(repo)[0]) == "pass"


def test_baseline_a_failure_that_ran_no_test_stays_a_failure(
    repo: Path, posix: str
) -> None:
    _write_plan(repo, _full_plan(("a.txt", '`echo "no tests ran"; exit 5`', "none")))
    assert _baseline(repo, posix, "1", "first").returncode == 0
    assert _result(_baseline_lines(repo)[0]) == "fail 5: no tests ran"


def test_baseline_copies_a_pass_that_ran_no_test(repo: Path, posix: str) -> None:
    verify = '`echo "collected 0 items" >> runs.txt; cat runs.txt`'
    _write_plan(repo, _full_plan(("a.txt", verify, "none"), ("a.txt", verify, "none")))
    assert _baseline(repo, posix, "1", "first").returncode == 0
    assert _baseline(repo, posix, "2").returncode == 0
    assert [_result(line) for line in _baseline_lines(repo)] == [
        "pass (no test ran)",
        "pass (no test ran)",
    ]
    assert (repo / "runs.txt").read_text() == "collected 0 items\n"


def _accept(
    repo: Path, posix: str, item: str, *extra: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return _run(repo, posix, "accept", "plans/p.md", item, *extra, env=env)


def test_accept_writes_a_record(repo: Path, posix: str) -> None:
    _write_plan(repo, _full_plan(("a.txt", "`true`", "none"), checks=("`echo ok`",)))
    run = _accept(repo, posix, "1")
    assert run.returncode == 0, run.stderr
    assert run.stdout == "accept: literal\nok\n"
    assert (repo / _STEPS / "acceptance.1").read_text().splitlines() == [
        "command: echo ok",
        "exit: 0",
        f"tree: {_snapshot(repo)}",
        "--- last 40 lines of output",
        "ok",
    ]


@pytest.mark.parametrize(
    ("check", "code"), [("`exit 5`", "5"), ("`nvm use`", "127"), ("`exit 124`", "124")]
)
def test_accept_a_failing_check_exits_1(
    repo: Path, posix: str, check: str, code: str
) -> None:
    _write_plan(repo, _full_plan(("a.txt", "`true`", "none"), checks=(check,)))
    run = _accept(repo, posix, "1")
    assert run.returncode == 1, run.stdout + run.stderr
    record = (repo / _STEPS / "acceptance.1").read_text()
    assert _field(record, "exit") == [code]
    assert "timed out" not in record


@pytest.mark.parametrize("check", ["manual: open the page", "the page `looks` right"])
def test_accept_a_manual_check_is_not_literal(
    repo: Path, posix: str, check: str
) -> None:
    _write_plan(repo, _full_plan(("a.txt", "`true`", "none"), checks=(check,)))
    run = _accept(repo, posix, "1")
    assert (run.returncode, run.stdout) == (0, "accept: not literal\n")
    assert (repo / _STEPS / "acceptance.1").read_text() == "exit: not literal\n"


def test_accept_finds_the_item_and_its_check(repo: Path, posix: str) -> None:
    text = _full_plan(("a.txt", "`touch step`", "none"), checks=("`touch one`",))
    text += "2. Item two, on two lines\n   - **Check:** `touch two`\n3. No check\n"
    _write_plan(repo, text)
    assert _accept(repo, posix, "2").returncode == 0
    assert (repo / "two").exists()
    assert not (repo / "one").exists()
    assert not (repo / "step").exists()
    run = _accept(repo, posix, "3")
    assert (run.returncode, run.stdout) == (0, "accept: not literal\n")
    assert _accept(repo, posix, "4").returncode == 64


def test_accept_timeout_exits_124(repo: Path, posix: str, marker: str) -> None:
    _write_plan(
        repo, _full_plan(("a.txt", "`true`", "none"), checks=(f"`sleep {marker}`",))
    )
    run = _accept(repo, posix, "1", env={"WHYGRAPH_VERIFY_TIMEOUT": "1"})
    assert run.returncode == 124, run.stdout + run.stderr
    assert _sleeps(marker) == []
    record = (repo / _STEPS / "acceptance.1").read_text()
    assert _field(record, "exit") == ["124"]
    assert "timed out after 1 s" in record.splitlines()


def test_accept_reads_the_plan_from_a_blob(repo: Path, posix: str) -> None:
    saved = _full_plan(("a.txt", "`true`", "none"), checks=("`touch right`",))
    blob = _git(repo, "hash-object", "-w", "--stdin", stdin=saved)
    _write_plan(repo, _full_plan(("a.txt", "`true`", "none"), checks=("`touch no`",)))
    assert _accept(repo, posix, "1", blob).returncode == 0
    assert (repo / "right").exists()
    assert not (repo / "no").exists()


@pytest.mark.parametrize("command", ["baseline", "accept"])
@pytest.mark.parametrize(("shell", "form"), _forms())
def test_baseline_and_accept_term_kills_the_job_and_exits_143(
    repo: Path, marker: str, shell: str, form: str, command: str
) -> None:
    span = f"`sleep {marker}`"
    _write_plan(repo, _full_plan(("a.txt", span, "none"), checks=(span,)))
    proc = subprocess.Popen(
        [*_SHELLS[shell], str(_SCRIPT), command, "plans/p.md", "1"],
        cwd=repo,
        env={**_env(), "WHYGRAPH_KILL_FORM": form},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not _sleeps(marker) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _sleeps(marker)
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=20)
    finally:
        proc.kill()

    assert proc.returncode == 143, err
    time.sleep(0.2)
    assert _sleeps(marker) == []


# ---- attempt, amends ---------------------------------------------------------


def _attempt(
    repo: Path, posix: str, *args: str, step: str = "1"
) -> subprocess.CompletedProcess[str]:
    return _run(repo, posix, "attempt", "plans/p.md", step, *args)


def _attempts(repo: Path) -> list[str]:
    path = repo / _STEPS / "attempts"
    return path.read_text().splitlines() if path.exists() else []


@pytest.mark.parametrize(
    ("args", "cap"),
    [
        (("retry", "sonnet:opus"), 1),
        (("constraint",), 1),
        (("findings", "0"), 0),
        (("findings", "1"), 1),
        (("findings", "2"), 2),
    ],
    ids=lambda v: "-".join(v) if isinstance(v, tuple) else str(v),
)
def test_attempt_caps(repo: Path, posix: str, args: tuple[str, ...], cap: int) -> None:
    _write_plan(repo, _steps_plan(("a.txt", "`true`"), ("a.txt", "`true`")))
    for used in range(cap):
        run = _attempt(repo, posix, *args)
        assert run.returncode == 0, run.stderr
        assert run.stdout == f"attempt: {args[0]} {used + 1} of {cap}\n"
    before = _attempts(repo)
    run = _attempt(repo, posix, *args)
    assert run.returncode == 1, run.stderr
    assert "cap" in run.stdout
    # At the cap, nothing is appended.
    assert _attempts(repo) == before
    # Another step has its own count.
    assert _attempt(repo, posix, *args, step="2").returncode == (0 if cap else 1)


def test_attempt_lines(repo: Path, posix: str) -> None:
    _write_plan(repo, _steps_plan(("a.txt", "`true`")))
    assert _attempt(repo, posix, "retry", "sonnet:opus").returncode == 0
    assert _attempt(repo, posix, "findings", "2").returncode == 0
    assert _attempt(repo, posix, "constraint").returncode == 0
    assert _attempt(repo, posix, "reset").returncode == 0
    assert _attempts(repo) == [
        "1 retry sonnet:opus",
        "1 findings",
        "1 constraint",
        "1 reset",
    ]


def test_attempt_begin_lines_count_for_nothing(repo: Path, posix: str) -> None:
    _write_plan(repo, _steps_plan(("a.txt", "`true`")))
    _begin(repo, posix)
    _begin(repo, posix)
    assert _attempt(repo, posix, "retry", "haiku:sonnet").returncode == 0
    assert _attempt(repo, posix, "retry", "haiku:sonnet").returncode == 1


def test_attempt_reset_keeps_lines_and_restarts_every_count(
    repo: Path, posix: str
) -> None:
    _write_plan(repo, _steps_plan(("a.txt", "`true`"), ("a.txt", "`true`")))
    assert _attempt(repo, posix, "retry", "sonnet:opus").returncode == 0
    assert _attempt(repo, posix, "constraint", step="2").returncode == 0
    assert _attempt(repo, posix, "retry", "sonnet:opus").returncode == 1
    # A reset line for step 1 restarts the counts of every step.
    assert _attempt(repo, posix, "reset").returncode == 0
    assert _attempt(repo, posix, "retry", "sonnet:opus").returncode == 0
    assert _attempt(repo, posix, "constraint", step="2").returncode == 0
    assert _attempts(repo) == [
        "1 retry sonnet:opus",
        "2 constraint",
        "1 reset",
        "1 retry sonnet:opus",
        "2 constraint",
    ]


@pytest.mark.parametrize(
    "args",
    [
        ("retry",),
        ("retry", "sonnet->opus"),
        ("retry", "Sonnet:Opus"),
        ("retry", "a b"),
        ("retry", "x;y"),
        ("retry", ""),
        ("findings",),
        ("findings", "3"),
        ("findings", "-1"),
        ("findings", "01"),
        ("findings", "x"),
        ("findings", "sonnet:opus"),
        ("findings", "1", "sonnet:opus"),
        ("constraint", "x"),
        ("reset", "x"),
        ("retry", "a:b", "c"),
        ("other",),
        (),
    ],
    ids=lambda a: "-".join(a) or "none",
)
def test_attempt_usage_errors(repo: Path, posix: str, args: tuple[str, ...]) -> None:
    _write_plan(repo, _steps_plan(("a.txt", "`true`")))
    run = _attempt(repo, posix, *args)
    assert run.returncode == 64, run.stdout + run.stderr
    assert not (repo / _STEPS / "attempts").exists()
    # The detail of a redirect never makes a file.
    assert not (repo / "opus").exists()


def _amendments(*lines: str) -> str:
    return "\n## Amendments\n\n" + "".join(f"{line}\n" for line in lines)


@pytest.mark.parametrize(
    ("lines", "step", "code"),
    [
        ((), "1", 0),
        (("- Step 2: Verify - `a` -> `b` (t)",), "1", 0),
        (("- Step 1: Verify - `a` -> `b` (t)",), "1", 1),
        (
            (
                "- Step 2: Verify - `a` -> `b` (t)",
                "- Step 3: Files - a -> a, b (t)",
            ),
            "1",
            0,
        ),
        (
            (
                "- Step 2: Verify - `a` -> `b` (t)",
                "- Step 3: Files - a -> a, b (t)",
                "- Step 4: Files - a -> a, b (t)",
            ),
            "1",
            1,
        ),
        (("- something else",) * 3, "1", 1),
    ],
)
def test_amends_counts_the_plan_section(
    repo: Path, posix: str, lines: tuple[str, ...], step: str, code: int
) -> None:
    steps = [("a.txt", "`true`")] * 4
    _write_plan(repo, _steps_plan(*steps) + (_amendments(*lines) if lines else ""))
    run = _run(repo, posix, "amends", "plans/p.md", step)
    assert run.returncode == code, run.stdout + run.stderr


def test_amends_reads_the_blob(repo: Path, posix: str) -> None:
    saved = _steps_plan(("a.txt", "`true`")) + _amendments(
        "- Step 1: Verify - `a` -> `b` (t)"
    )
    blob = _git(repo, "hash-object", "-w", "--stdin", stdin=saved)
    _write_plan(repo, _steps_plan(("a.txt", "`true`")))
    assert _run(repo, posix, "amends", "plans/p.md", "1").returncode == 0
    assert _run(repo, posix, "amends", "plans/p.md", "1", blob).returncode == 1


def test_amends_are_never_reset(repo: Path, posix: str) -> None:
    """Amend, reset, amend again: refused, because the plan holds the count."""
    _write_plan(
        repo,
        _steps_plan(("a.txt", "`true`"))
        + _amendments("- Step 1: Verify - `a` -> `b` (t)"),
    )
    assert _attempt(repo, posix, "reset").returncode == 0
    assert _run(repo, posix, "amends", "plans/p.md", "1").returncode == 1


def test_amends_ignores_a_fenced_section(repo: Path, posix: str) -> None:
    text = _steps_plan(("a.txt", "`true`"))
    text += "\n```\n## Amendments\n- Step 1: Verify - `a` -> `b` (t)\n```\n"
    _write_plan(repo, text)
    assert _run(repo, posix, "amends", "plans/p.md", "1").returncode == 0


# ---- save, restore, log-check, state -------------------------------------------


def _save(repo: Path, posix: str) -> str:
    run = _run(repo, posix, "save", "plans/p.md")
    assert run.returncode == 0, run.stderr
    blob = run.stdout.strip()
    assert _HEX40.match(blob)
    return blob


def test_save_and_restore_round_trip(repo: Path, posix: str) -> None:
    text = _steps_plan(("a.txt", "`true`"))
    _write_plan(repo, text)
    blob = _save(repo, posix)
    assert _git(repo, "cat-file", "blob", blob) + "\n" == text
    (repo / "plans" / "p.md").write_text("rewritten\n")
    run = _run(repo, posix, "restore", "plans/p.md", blob)
    assert run.returncode == 0, run.stderr
    assert (repo / "plans" / "p.md").read_text() == text
    # A deleted plan comes back too.
    (repo / "plans" / "p.md").unlink()
    assert _run(repo, posix, "restore", "plans/p.md", blob).returncode == 0
    assert (repo / "plans" / "p.md").read_text() == text
    assert not list((repo / "plans").glob(".plan-tool.*"))


@pytest.mark.parametrize("command", ["save", "restore", "log-check", "amend-check"])
def test_a_symlinked_plan_is_refused(repo: Path, posix: str, command: str) -> None:
    text = _steps_plan(("a.txt", "`true`"))
    _write_plan(repo, text)
    blob = _save(repo, posix)
    target = repo.parent / "elsewhere.md"
    target.write_text("keep\n")
    plan = repo / "plans" / "p.md"
    plan.unlink()
    plan.symlink_to(target)
    args = {
        "save": (),
        "restore": (blob,),
        "log-check": (blob,),
        "amend-check": (blob, "1"),
    }[command]
    run = _run(repo, posix, command, "plans/p.md", *args)
    assert run.returncode == 77, run.stdout + run.stderr
    assert plan.is_symlink()
    assert target.read_text() == "keep\n"


_LOG_PLAN = _steps_plan(("a.txt", "`true`"), ("a.txt", "`true`"))


def _log_check(repo: Path, posix: str, blob: str) -> subprocess.CompletedProcess[str]:
    return _run(repo, posix, "log-check", "plans/p.md", blob)


@pytest.mark.parametrize("final_newline", [True, False])
def test_log_check_passes_a_first_log_write(
    repo: Path, posix: str, final_newline: bool
) -> None:
    text = _LOG_PLAN if final_newline else _LOG_PLAN.rstrip("\n")
    _write_plan(repo, text)
    blob = _save(repo, posix)
    written = text.rstrip("\n") + "\n\n## Implementation log\n\n- [x] Step 1: done\n"
    _write_plan(repo, written)
    run = _log_check(repo, posix, blob)
    assert run.returncode == 0, run.stdout + run.stderr
    new = run.stdout.strip()
    assert _HEX40.match(new)
    assert new != blob
    # The new id is the checked copy, stored.
    assert _git(repo, "cat-file", "blob", new) + "\n" == written


def test_log_check_passes_an_edit_inside_the_log(repo: Path, posix: str) -> None:
    text = _LOG_PLAN + "\n## Implementation log\n\n- [x] Step 1: done\n"
    _write_plan(repo, text)
    blob = _save(repo, posix)
    _write_plan(repo, text.replace("- [x] Step 1: done", "- [x] Step 1: redone\n- x"))
    assert _log_check(repo, posix, blob).returncode == 0


def test_log_check_passes_a_log_above_another_section(repo: Path, posix: str) -> None:
    text = _LOG_PLAN + "\n## Implementation log\n\n- one\n\n## Later\nkept\n"
    _write_plan(repo, text)
    blob = _save(repo, posix)
    _write_plan(repo, text.replace("- one\n", "- one\n- two\n"))
    assert _log_check(repo, posix, blob).returncode == 0
    _write_plan(repo, text.replace("kept", "changed"))
    run = _log_check(repo, posix, blob)
    assert run.returncode == 1
    assert "changed outside the log: changed" in run.stdout


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("**Verify:** `true`", "**Verify:** `false`"),
        ("1. Done - **Check:** `true`", "1. Done - **Check:** `false`"),
        ("- Step 2: Verify - `a` -> `b` (t)\n", ""),
        ("## Steps\n", "## Steps\n\n"),
        ("# Plan: Test\n", "# Plan: Test\nextra\n"),
    ],
    ids=["verify", "acceptance", "amendment", "blank", "added"],
)
def test_log_check_fails_an_edit_outside_the_log(
    repo: Path, posix: str, old: str, new: str
) -> None:
    text = (
        _LOG_PLAN
        + _amendments("- Step 2: Verify - `a` -> `b` (t)")
        + "\n## Implementation log\n\n- [x] Step 1: done\n"
    )
    _write_plan(repo, text)
    blob = _save(repo, posix)
    _write_plan(repo, text.replace(old, new, 1))
    run = _log_check(repo, posix, blob)
    assert run.returncode == 1, run.stdout + run.stderr
    assert run.stdout.startswith("log-check: line ")
    assert len(run.stdout.splitlines()) == 1


def test_log_check_fails_a_missing_plan(repo: Path, posix: str) -> None:
    _write_plan(repo, _LOG_PLAN)
    blob = _save(repo, posix)
    (repo / "plans" / "p.md").unlink()
    run = _log_check(repo, posix, blob)
    assert run.returncode == 1
    assert "missing" in run.stdout


@pytest.mark.parametrize(
    "inserted",
    [
        # A second log section that swallows the steps.
        "## Implementation log\n",
        # A fence in a new log section, which would hide the Steps heading.
        "## Implementation log\n```\n",
    ],
    ids=["heading", "fence"],
)
def test_log_check_fails_a_log_section_that_hides_the_plan(
    repo: Path, posix: str, inserted: str
) -> None:
    _write_plan(repo, _LOG_PLAN)
    blob = _save(repo, posix)
    _write_plan(repo, _LOG_PLAN.replace("## Steps\n", inserted + "## Steps\n", 1))
    assert _log_check(repo, posix, blob).returncode == 1


def _replace_blob(repo: Path, blob: str, text: str) -> None:
    other = _git(repo, "hash-object", "-w", "--stdin", stdin=text)
    _git(repo, "replace", "-f", blob, other)


def _overwrite_loose_object(repo: Path, blob: str, text: str) -> None:
    other = _git(repo, "hash-object", "-w", "--stdin", stdin=text)
    objects = repo / ".git" / "objects"
    path = objects / blob[:2] / blob[2:]
    path.chmod(0o644)
    path.write_bytes((objects / other[:2] / other[2:]).read_bytes())


@pytest.mark.parametrize("tamper", [_replace_blob, _overwrite_loose_object])
@pytest.mark.parametrize("command", ["log-check", "restore", "amend-check"])
def test_a_changed_blob_exits_65(
    repo: Path, posix: str, tamper: object, command: str
) -> None:
    text = _steps_plan(("a.txt", "`true`"))
    _write_plan(repo, text)
    blob = _save(repo, posix)
    tamper(repo, blob, text.replace("`true`", "`touch ran`"))  # type: ignore[operator]
    args = (blob, "1") if command == "amend-check" else (blob,)
    run = _run(repo, posix, command, "plans/p.md", *args)
    assert run.returncode == 65, run.stdout + run.stderr
    assert "the saved plan was changed" in run.stderr
    assert (repo / "plans" / "p.md").read_text() == text


def _state(repo: Path, posix: str, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(repo, posix, "state", "plans/p.md", *args)


def test_state_with_its_own_hash_exits_0(repo: Path, posix: str) -> None:
    _write_plan(repo, _steps_plan(("a.txt", "`true`")))
    _steps_file(repo, "attempts", "1 begin x\n")
    run = _state(repo, posix)
    assert run.returncode == 0, run.stderr
    hash_ = run.stdout.strip()
    assert _HEX40.match(hash_)
    # The implementor's own trees and records do not count.
    _steps_file(repo, "1.t0", "x\n")
    _steps_file(repo, "1.verify", "exit: 0\n")
    (repo / "plans" / "p.md").write_text("changed\n")
    assert _state(repo, posix, hash_).returncode == 0


def _append_reset(repo: Path) -> None:
    with (repo / _STEPS / "attempts").open("a") as attempts:
        attempts.write("1 reset\n")


def _new_done(repo: Path) -> None:
    _steps_file(repo, "2.done", f"{_EMPTY_TREE}\n")


def _changed_baseline(repo: Path) -> None:
    _steps_file(repo, "baseline", "step 1 x: pass\n")


def _changed_hook(repo: Path) -> None:
    (repo / ".git" / "hooks" / "post-checkout").write_text("#!/bin/sh\n")


def _executable_hook(repo: Path) -> None:
    (repo / ".git" / "hooks" / "pre-commit").chmod(0o755)


def _changed_config(repo: Path) -> None:
    _git(repo, "config", "core.hooksPath", "elsewhere")


@pytest.mark.parametrize(
    "change",
    [
        _append_reset,
        _new_done,
        _changed_baseline,
        _changed_hook,
        _executable_hook,
        _changed_config,
    ],
)
def test_state_sees_a_change(repo: Path, posix: str, change: object) -> None:
    _write_plan(repo, _steps_plan(("a.txt", "`true`")))
    _steps_file(repo, "attempts", "1 begin x\n")
    _steps_file(repo, "baseline", "step 1 x: fail 1: no\n")
    _steps_file(repo, "1.done", f"{_EMPTY_TREE}\n")
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    (hooks / "pre-commit").chmod(0o644)
    hash_ = _state(repo, posix).stdout.strip()
    change(repo)  # type: ignore[operator]
    run = _state(repo, posix, hash_)
    assert run.returncode == 1, run.stdout + run.stderr
    assert run.stdout == "state: changed\n"


# ---- amend-check -----------------------------------------------------------------

_C15_CHANGE = (
    'Add the constant `FOOTER = "Footer"` after `TITLE` in `mini/report.py`. '
    "Append the line `# gate` to the end of `mini/flags.py`."
)
_AMEND_PLAN = f"""\
# Plan: Amend

## Steps

1. **Gate the flags**
   - **Files:** mini/report.py:1-5, tests/test_a.py
   - **Change:** {_C15_CHANGE}
     Keep `src/locked.py` and `src/frozen/x.py` as they are; also add `.claude/x.md`, `src/`, `.` and `src/new.py`.
   - **Boundaries:** `src/locked.py`, `src/frozen/`
   - **Test first:** `tests/test_a.py::test_one` - fails before the change
   - **Verify:** `uv run pytest tests/test_a.py::test_one`
2. **Run the script**
   - **Files:** scripts/a.sh
   - **Change:** Edit `scripts/a.sh`.
   - **Boundaries:** none
   - **Test first:** none
   - **Verify:** `sh scripts/a.sh` and `npm run test`
3. **Test c**
   - **Files:** tests/test_c.py
   - **Change:** Add `tests/test_c.py`.
   - **Boundaries:** none
   - **Test first:** none
   - **Verify:** `uv run pytest -q tests/test_c.py::test_x`
4. **Test b**
   - **Files:** tests/test_b.py, tests/test_d.py (new)
   - **Change:** Edit `tests/test_b.py`.
   - **Boundaries:** none
   - **Test first:** none
   - **Verify:** `uv run pytest tests/test_b.py`

## Acceptance
1. The suite passes - **Check:** `uv run pytest`

## Implementation log

- [x] Step 1: done
"""


@pytest.fixture
def amend_repo(repo: Path) -> Path:
    """The repository of ``_AMEND_PLAN``: tracked tests and scripts, an
    ignored test, a tracked symlink, an untracked script and a directory."""
    for path in (
        "tests/test_a.py",
        "tests/test_b.py",
        "tests/test_c.py",
        "tests/test_e.py",
        "scripts/a.sh",
        "scripts/c.sh",
        "scripts/x.py",
        "mini/report.py",
        "deploy/x.txt",
    ):
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text("x\n")
    (repo / "tests" / "link.py").symlink_to("test_b.py")
    with (repo / ".gitignore").open("a") as ignore:
        ignore.write("build/\n")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-q", "-m", "amend fixture")
    (repo / "build").mkdir()
    (repo / "build" / "test_ok.py").write_text("x\n")
    (repo / "scripts" / "b.sh").write_text("x\n")
    (repo / "tests" / "test_d.py").write_text("x\n")
    return repo


def _amend(
    repo: Path, posix: str, old: str, new: str, step: str, *, plan: str = _AMEND_PLAN
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Save ``plan``, change ``old`` to ``new`` in it, then run amend-check."""
    _write_plan(repo, plan)
    blob = _save(repo, posix)
    assert old in plan
    amended = plan.replace(old, new, 1)
    _write_plan(repo, amended)
    run = _run(repo, posix, "amend-check", "plans/p.md", blob, step)
    return run, amended


def _verify_line(step: str) -> str:
    return {
        "1": "**Verify:** `uv run pytest tests/test_a.py::test_one`",
        "2": "**Verify:** `sh scripts/a.sh` and `npm run test`",
        "3": "**Verify:** `uv run pytest -q tests/test_c.py::test_x`",
        "4": "**Verify:** `uv run pytest tests/test_b.py`",
    }[step]


_PASSES = [
    pytest.param(
        "3",
        _verify_line("3"),
        "**Verify:** `uv run pytest -q tests/test_b.py::test_y`",
        id="another-node-id",
    ),
    pytest.param(
        "4",
        _verify_line("4"),
        "**Verify:** `uv run   pytest\ttests/test_e.py`",
        id="another-path-blanks-normalized",
    ),
    pytest.param(
        "4",
        _verify_line("4"),
        "**Verify:** `uv run pytest tests/test_d.py`",
        id="a-path-named-in-files",
    ),
    pytest.param(
        "2",
        _verify_line("2"),
        "**Verify:** `sh scripts/c.sh` and `npm run test`",
        id="script-re-pointed",
    ),
    pytest.param(
        "4",
        _verify_line("4"),
        "**Verify:** `uv run pytest`",
        id="an-acceptance-check",
    ),
    pytest.param(
        "1",
        "**Verify:** `uv run pytest tests/test_a.py::test_one`",
        "**Verify:** `uv run pytest -q tests/test_a.py::test_one`",
        id="test-first-id-kept",
    ),
    pytest.param(
        "1",
        "**Files:** mini/report.py:1-5, tests/test_a.py",
        "**Files:** mini/report.py:1-5, tests/test_a.py, mini/flags.py",
        id="c15-files-path",
    ),
    pytest.param(
        "1",
        "**Files:** mini/report.py:1-5, tests/test_a.py",
        "**Files:** mini/report.py:1-5, tests/test_a.py, `src/new.py` (new)",
        id="files-path-from-the-change-paragraph",
    ),
]


@pytest.mark.parametrize(("step", "old", "new"), _PASSES)
def test_amend_check_passes(
    amend_repo: Path, posix: str, step: str, old: str, new: str
) -> None:
    run, amended = _amend(amend_repo, posix, old, new, step)
    assert run.returncode == 0, run.stdout + run.stderr
    blob = run.stdout.strip()
    assert _HEX40.match(blob)
    text = (amend_repo / "plans" / "p.md").read_text()
    assert _git(amend_repo, "cat-file", "blob", blob) + "\n" == text
    field = "Verify" if "Verify" in old else "Files"
    old_value = old.split(":** ", 1)[1]
    new_value = new.split(":** ", 1)[1].strip()
    entry = re.search(rf"^- Step {step}: {field} - (.*)$", text, re.M)
    assert entry is not None
    assert entry.group(1).startswith(f"{old_value} -> {new_value} (")
    assert re.search(r"\(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\)$", entry.group(1))
    # The plan is the amended text plus the entry, nothing else.
    assert text.replace(entry.group(0) + "\n", "") == amended.replace(
        "\n## Implementation log", "\n## Amendments\n\n\n## Implementation log"
    )
    assert _run(amend_repo, posix, "amends", "plans/p.md", step).returncode == 1


def test_amend_check_creates_the_section_above_the_log(
    amend_repo: Path, posix: str
) -> None:
    run, amended = _amend(
        amend_repo,
        posix,
        _verify_line("4"),
        "**Verify:** `uv run pytest tests/test_e.py`",
        "4",
    )
    assert run.returncode == 0, run.stdout + run.stderr
    text = (amend_repo / "plans" / "p.md").read_text()
    head, log = text.split("## Implementation log\n")
    assert head.endswith(
        "## Acceptance\n1. The suite passes - **Check:** `uv run pytest`\n\n"
        "## Amendments\n\n"
        "- Step 4: Verify - `uv run pytest tests/test_b.py` -> "
        f"`uv run pytest tests/test_e.py` ({head.rsplit('(', 1)[1]}"
    )
    assert head.endswith(")\n\n")
    assert log == "\n- [x] Step 1: done\n"
    # The plan with the entry passes check's structure rules for the section.
    check = _run(amend_repo, posix, "check", "plans/p.md")
    assert "Amendments" not in check.stdout


def test_amend_check_appends_to_the_section_and_at_the_end(
    amend_repo: Path, posix: str
) -> None:
    plan = _AMEND_PLAN.split("\n## Implementation log")[0]
    run, _ = _amend(
        amend_repo,
        posix,
        _verify_line("4"),
        "**Verify:** `uv run pytest tests/test_e.py`",
        "4",
        plan=plan,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    text = (amend_repo / "plans" / "p.md").read_text()
    new_line = "**Verify:** `uv run pytest tests/test_e.py`"
    assert text.startswith(
        plan.replace(_verify_line("4"), new_line)
        + "\n## Amendments\n\n- Step 4: Verify - "
    )
    blob = run.stdout.strip()
    second = text.replace(
        _verify_line("3"), "**Verify:** `uv run pytest -q tests/test_b.py::test_y`"
    )
    _write_plan(amend_repo, second)
    run = _run(amend_repo, posix, "amend-check", "plans/p.md", blob, "3")
    assert run.returncode == 0, run.stdout + run.stderr
    lines = (amend_repo / "plans" / "p.md").read_text().splitlines()
    assert lines[-2].startswith("- Step 4: Verify - ")
    assert lines[-1].startswith("- Step 3: Verify - ")


_STEP3_MIDDLE = (
    "   - **Change:** Add `tests/test_c.py`.\n"
    "   - **Boundaries:** none\n"
    "   - **Test first:** none\n"
    "   - "
)
_FAILS = [
    pytest.param(
        "3",
        "**Files:** tests/test_c.py\n" + _STEP3_MIDDLE + _verify_line("3"),
        "**Files:** tests/test_c.py, tests/test_b.py\n"
        + _STEP3_MIDDLE
        + "**Verify:** `uv run pytest -q tests/test_b.py::test_y`",
        id="both-lines",
    ),
    pytest.param(
        "1",
        "**Files:** mini/report.py:1-5, tests/test_a.py",
        "**Files:** mini/report.py:1-5, tests/test_a.py, mini/flags.pyc",
        id="c15-pyc",
    ),
    pytest.param(
        "4",
        _verify_line("4"),
        "**Verify:** `uv run scripts/x.py tests/test_b.py`",
        id="a-program-token",
    ),
    pytest.param(
        "2",
        _verify_line("2"),
        "**Verify:** `sh scripts/b.sh` and `npm run test`",
        id="untracked-script-in-no-files",
    ),
    pytest.param(
        "4",
        _verify_line("4"),
        "**Verify:** `uv run pytest build/test_ok.py`",
        id="gitignored",
    ),
    pytest.param(
        "4",
        _verify_line("4"),
        "**Verify:** `uv run pytest tests/link.py`",
        id="tracked-symlink",
    ),
    pytest.param(
        "3",
        _verify_line("3"),
        "**Verify:** `uv run pytest tests/test_b.py tests/test_c.py::test_x`",
        id="flag-to-path",
    ),
    pytest.param(
        "2",
        _verify_line("2"),
        "**Verify:** `sh scripts/a.sh` and `npm run deploy`",
        id="npm-run-deploy-with-a-directory",
    ),
    pytest.param(
        "1",
        "**Change:** Add the constant",
        "**Change:** Add a constant",
        id="change-edit",
    ),
    pytest.param(
        "1",
        "**Boundaries:** `src/locked.py`, `src/frozen/`",
        "**Boundaries:** none",
        id="boundaries-edit",
    ),
    pytest.param(
        "1",
        _verify_line("4"),
        "**Verify:** `uv run pytest tests/test_e.py`",
        id="another-steps-line",
    ),
    pytest.param(
        "1",
        "**Files:** mini/report.py:1-5, tests/test_a.py",
        "**Files:** mini/report.py:1-5",
        id="removed-files-path",
    ),
    pytest.param(
        "4", _verify_line("4"), "**Verify:** manual: run the tests", id="manual"
    ),
    pytest.param(
        "4", _verify_line("4"), "**Verify:** run the tests and look", id="prose"
    ),
    pytest.param("4", _verify_line("4"), "**Verify:** `true`", id="true"),
    pytest.param("4", _verify_line("4"), "**Verify:** `echo ok`", id="echo"),
    pytest.param(
        "4", _verify_line("4"), "**Verify:** `npm test || true`", id="or-true"
    ),
    pytest.param("4", _verify_line("4"), "**Verify:** `a; b`", id="semicolon"),
    pytest.param(
        "4",
        _verify_line("4"),
        "**Verify:** `uv run pytest $(x)`",
        id="substitution",
    ),
    pytest.param("4", _verify_line("4"), "**Verify:** `git push`", id="git-push"),
    pytest.param(
        "4",
        _verify_line("4"),
        "**Verify:** `git -C . push origin`",
        id="git-option-push",
    ),
    pytest.param(
        "4",
        _verify_line("4"),
        "**Verify:** `uv run true tests/test_b.py`",
        id="uv-run-true",
    ),
    pytest.param(
        "4",
        _verify_line("4"),
        "**Verify:** `uv run python -c pass`",
        id="python-c",
    ),
    pytest.param(
        "4",
        _verify_line("4"),
        "**Verify:** `uv run pytest --co -q tests/test_b.py`",
        id="an-added-flag",
    ),
    pytest.param(
        "4", _verify_line("4"), "**Verify:** `npm run deploy`", id="npm-run-deploy"
    ),
    pytest.param(
        "3",
        _verify_line("3"),
        "**Verify:** `uv run pytest -q tests/test_c.py::test_x[a]`",
        id="parametrized-id",
    ),
    pytest.param(
        "1",
        _verify_line("1"),
        "**Verify:** `uv run pytest tests/test_a.py::test_two`",
        id="dropped-test-first-id",
    ),
    pytest.param(
        "1",
        "**Files:** mini/report.py:1-5, tests/test_a.py",
        "**Files:** mini/report.py:1-5, tests/test_a.py, src/locked.py",
        id="files-path-in-boundaries",
    ),
    pytest.param(
        "1",
        "**Files:** mini/report.py:1-5, tests/test_a.py",
        "**Files:** mini/report.py:1-5, tests/test_a.py, src/frozen/x.py",
        id="files-path-under-a-boundaries-directory",
    ),
    pytest.param(
        "1",
        "**Files:** mini/report.py:1-5, tests/test_a.py",
        "**Files:** mini/report.py:1-5, tests/test_a.py, src/",
        id="files-directory",
    ),
    pytest.param(
        "1",
        "**Files:** mini/report.py:1-5, tests/test_a.py",
        "**Files:** mini/report.py:1-5, tests/test_a.py, .",
        id="files-dot",
    ),
    pytest.param(
        "1",
        "**Files:** mini/report.py:1-5, tests/test_a.py",
        "**Files:** mini/report.py:1-5, tests/test_a.py, .claude/x.md",
        id="files-under-claude",
    ),
    pytest.param(
        "4",
        _verify_line("4"),
        "**Verify:** `uv run pytest .claude/x.py`",
        id="verify-under-claude",
    ),
    pytest.param(
        "4",
        _verify_line("4"),
        "**Verify:** `uv run pytest tests/test_b.py` and `uv run pytest tests/test_e.py`",
        id="an-added-command",
    ),
    pytest.param(
        "2",
        _verify_line("2"),
        "**Verify:** `sh scripts/a.sh`",
        id="a-dropped-command",
    ),
    pytest.param(
        "2",
        _verify_line("2"),
        "**Verify:** `sh scripts/c.sh` and `uv run pytest`",
        id="two-commands-changed",
    ),
    pytest.param(
        "4",
        "## Acceptance\n",
        "## Acceptance\nextra\n",
        id="an-added-line",
    ),
    pytest.param(
        "4",
        "- **Verify:** `uv run pytest tests/test_b.py`",
        "- **Verify:**  `uv run pytest tests/test_b.py` ",
        id="blanks-only",
    ),
    pytest.param(
        "4",
        "- **Verify:** `uv run pytest tests/test_b.py`",
        "* **Verify:** `uv run pytest tests/test_e.py`",
        id="the-label-changed",
    ),
]


@pytest.mark.parametrize(("step", "old", "new"), _FAILS)
def test_amend_check_fails(
    amend_repo: Path, posix: str, step: str, old: str, new: str
) -> None:
    (amend_repo / ".claude").mkdir()
    (amend_repo / ".claude" / "x.py").write_text("x\n")
    _git(amend_repo, "add", "-f", ".claude/x.py")
    run, amended = _amend(amend_repo, posix, old, new, step)
    assert run.returncode == 1, run.stdout + run.stderr
    assert run.stdout
    assert all(line.startswith("amend-check: ") for line in run.stdout.splitlines())
    # A failing check writes nothing: the command restores the plan.
    assert (amend_repo / "plans" / "p.md").read_text() == amended


@pytest.mark.parametrize(
    ("reviewed", "new", "code"),
    [
        ("`LANG=C sh scripts/a.sh`", "`LANG=C sh scripts/c.sh`", 0),
        ("`LANG=C scripts/a.sh`", "`LANG=C scripts/c.sh`", 1),
        ("`scripts/a.sh`", "`scripts/c.sh`", 1),
    ],
    ids=["script-argument", "program-after-env", "program"],
)
def test_amend_check_never_swaps_the_program(
    amend_repo: Path, posix: str, reviewed: str, new: str, code: int
) -> None:
    plan = _AMEND_PLAN.replace(_verify_line("4"), f"**Verify:** {reviewed}")
    run, _ = _amend(
        amend_repo,
        posix,
        f"**Verify:** {reviewed}",
        f"**Verify:** {new}",
        "4",
        plan=plan,
    )
    assert run.returncode == code, run.stdout + run.stderr


def test_amend_check_fails_an_edit_to_the_amendments(
    amend_repo: Path, posix: str
) -> None:
    entry = "- Step 2: Verify - `a` -> `b` (t)"
    plan = _AMEND_PLAN.replace(
        "\n## Implementation log", _amendments(entry) + "\n## Implementation log"
    )
    for old, new in (
        (entry, "- Step 2: Verify - `a` -> `c` (t)"),
        (entry + "\n", ""),
        (
            _verify_line("4"),
            "**Verify:** `uv run pytest tests/test_e.py`\n"
            "\n## Amendments\n\n- Step 4: Verify - x -> y (t)",
        ),
    ):
        run, _ = _amend(amend_repo, posix, old, new, "4", plan=plan)
        assert run.returncode == 1, run.stdout + run.stderr


@pytest.mark.parametrize(
    ("entries", "step"),
    [
        (("- Step 4: Files - a -> a, b (t)",), "4"),
        (
            (
                "- Step 1: Files - a -> a, b (t)",
                "- Step 2: Files - a -> a, b (t)",
                "- Step 3: Files - a -> a, b (t)",
            ),
            "4",
        ),
    ],
    ids=["step-cap", "plan-cap"],
)
def test_amend_check_holds_the_caps(
    amend_repo: Path, posix: str, entries: tuple[str, ...], step: str
) -> None:
    plan = _AMEND_PLAN.replace(
        "\n## Implementation log", _amendments(*entries) + "\n## Implementation log"
    )
    run, _ = _amend(
        amend_repo,
        posix,
        _verify_line("4"),
        "**Verify:** `uv run pytest tests/test_e.py`",
        step,
        plan=plan,
    )
    assert run.returncode == 1
    assert "amendment" in run.stdout


def test_amend_check_fails_an_unchanged_plan(amend_repo: Path, posix: str) -> None:
    run, _ = _amend(amend_repo, posix, _verify_line("4"), _verify_line("4"), "4")
    assert run.returncode == 1
    assert "did not change" in run.stdout


def test_check_accepts_a_plan_with_amendments(tmp_path: Path) -> None:
    text = _CLEAN_PLAN.replace(
        "## Confidence: high",
        "## Amendments\n\n- Step 1: Verify - `true` -> `true` and `true` "
        "(2026-11-12T00:00:00Z)\n\n## Confidence: high",
    )
    run = _check(tmp_path, text)
    assert run.returncode == 0, run.stdout


def test_check_accepts_a_plan_with_a_plan_review(tmp_path: Path) -> None:
    text = _CLEAN_PLAN.replace(
        "## Confidence: high",
        "## Plan review\n\n- applied 1, refuted 1, asked 0, blocked 0, deferred 0\n"
        "- F1 (logic): applied - Verify step 1: `true` -> `true` and `true`\n"
        "- F2 (ui): refuted: the route is rendered at app.tsx:12\n"
        "- not checked: check 7\n\n## Confidence: high",
    )
    run = _check(tmp_path, text)
    assert run.returncode == 0, run.stdout


# ---- constraint checks ----------------------------------------------------------

_FORM = "Constraint check is not in the fixed form"
_CHARS = "Constraint check has a character outside the allowed set"
_E_ACUTE = "\u00e9"


def _with_checks(*lines: str) -> str:
    """The clean plan with a ``## Constraint checks`` section right before ``## Steps``."""
    section = "## Constraint checks\n\n" + "".join(f"{line}\n" for line in lines)
    return _replaced("## Steps\n", f"{section}\n## Steps\n")


@pytest.mark.parametrize(
    "line",
    [
        '- note #7 - absent: "time.sleep(" in src/**/*.py',
        '- note #12 - present: "raise ValueError" in src/a.py',
        '- note #3 - absent: "caf\u00e9 au lait" in docs/*.md',
        '- note #3 - absent: "$(touch x); rm -rf / | cat" in a-b_c.d/**',
        '- note #3 - absent: "abc" in *',
        '- note #3 - absent: "abc" in **/x/**',
        '- note #3 - present: "abc" in src/a.py  ',
        f'- note #3 - absent: "{"x" * 200}" in {"g" * 200}',
        f'- note #3 - absent: "{_E_ACUTE * 200}" in a',
    ],
)
def test_check_accepts_a_constraint_check(tmp_path: Path, line: str) -> None:
    run = _check(tmp_path, _with_checks(line))
    assert run.returncode == 0, run.stdout + run.stderr


def test_check_accepts_an_empty_constraint_section(tmp_path: Path) -> None:
    run = _check(tmp_path, _with_checks())
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('- note 7 - absent: "abc" in a', _FORM),
        ('- note #07 - absent: "abc" in a', _FORM),
        ('- note #x - absent: "abc" in a', _FORM),
        ('- note #7 - missing: "abc" in a', _FORM),
        ('- note #7 - absent "abc" in a', _FORM),
        ("- note #7 - absent: abc in a", _FORM),
        ('- note #7 - absent: "abc"', _FORM),
        ('- note #7 - absent: "abc" in ', _FORM),
        ('- note #7 - absent: "abc" within a', _FORM),
        ('* note #7 - absent: "abc" in a', _FORM),
        ('- note #7 - absent: "a"c" in a', _FORM),
        ("A sentence of prose.", _FORM),
        ('- note #7 - absent: "ab" in a', _FORM),
        (f'- note #7 - absent: "{"x" * 201}" in a', _FORM),
        (f'- note #7 - absent: "abc" in {"g" * 201}', _FORM),
        ('- note #7 - absent: "abc" in /src/a.py', _FORM),
        ('- note #7 - absent: "abc" in src/../a.py', _FORM),
        ('- note #7 - absent: "abc" in ..', _FORM),
        ('- note #7 - absent: "abc" in **/a/**/b/**', _FORM),
        ('- note #7 - absent: "a`b" in a', _CHARS),
        ('- note #7 - absent: "a\\b" in a', _CHARS),
        ('- note #7 - absent: "a\tb" in a', _CHARS),
        ('- note #7 - absent: "a\x01b" in a', _CHARS),
        ('- note #7 - absent: "a\x7fb" in a', _CHARS),
        ('- note #7 - absent: "a\u0085b" in a', _CHARS),
        ('- note #7 - absent: "a\u2028b" in a', _CHARS),
        ('- note #7 - absent: "a\u202eb" in a', _CHARS),
        ('- note #7 - absent: "abc" in src/a b.py', _CHARS),
        ('- note #7 - absent: "abc" in src/a?.py', _CHARS),
        ('- note #7 - absent: "abc" in src/[ab].py', _CHARS),
        ('- note #7 - absent: "abc" in src/$(x)', _CHARS),
        ('- note #7 - absent: "abc" in src/a;b', _CHARS),
        ('- note #7 - absent: "abc" in :(top)a', _CHARS),
        ('- note #7 - absent: "abc" in src/\u00e9.py', _CHARS),
    ],
)
def test_check_refuses_a_bad_constraint_check(
    tmp_path: Path, line: str, message: str
) -> None:
    text = _with_checks('- note #1 - absent: "fine" in a', line)
    # Not splitlines(): it also splits on U+0085 and U+2028, which awk does not.
    at = text.split("\n").index(line) + 1
    _assert_one_finding(tmp_path, text, at, message)


def test_check_reads_constraint_checks_only_in_their_section(tmp_path: Path) -> None:
    fenced = _with_checks("```", "- not a check", "```")
    assert _check(tmp_path, fenced).returncode == 0
    elsewhere = _replaced("A reason.\n", 'A reason.\n- note #7 - absent: "a`b" in a\n')
    assert _check(tmp_path, elsewhere).returncode == 0


def _constraints_plan(*checks: str, purpose: dict[int, str] | None = None) -> str:
    """A three-step plan with ``checks`` under ``## Constraint checks``.

    ``purpose`` maps a step number to the text of its **Changes on purpose** line.
    """
    section = (
        "## Constraint checks\n\n" + "".join(f"{c}\n" for c in checks) + "\n"
        if checks
        else ""
    )
    body = ""
    for n in (1, 2, 3):
        body += f"{n}. **Step {n}**\n   - **Files:** a.txt\n"
        if purpose and n in purpose:
            body += f"   - **Changes on purpose:** {purpose[n]}\n"
        body += "   - **Verify:** `true`\n"
    return (
        f"# Plan: Test\n\n{section}## Steps\n\n{body}\n"
        "## Acceptance\n1. Done - **Check:** `true`\n"
    )


def _constraints(
    repo: Path, posix: str, text: str, *args: str
) -> subprocess.CompletedProcess[str]:
    _write_plan(repo, text)
    return _run(repo, posix, "constraints", "plans/p.md", *args)


def _source(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "$(touch pwned)",
        "; touch pwned",
        "&& touch pwned #",
        "' ; touch pwned ; '",
        "| touch pwned",
        "--open-files-in-pager=touch pwned",
        "-Otouch pwned",
    ],
)
def test_constraints_searches_shell_text_literally(
    repo: Path, posix: str, text: str
) -> None:
    # Criterion 12: the text reaches git grep as an argument and is never run.
    _source(repo, "src/a.py", f"x = 1\ny = 2\nz = '{text}'\n")
    run = _constraints(
        repo, posix, _constraints_plan(f'- note #7 - absent: "{text}" in src/**')
    )
    assert run.returncode == 1, run.stdout + run.stderr
    assert run.stdout.splitlines() == [
        "broken: note #7 src/a.py:3",
        "constraints: 0 hold, 1 broken",
    ]
    assert not (repo / "pwned").exists()
    assert not (repo / "src" / "pwned").exists()
    assert not (repo.parent / "pwned").exists()


def test_constraints_a_shell_glob_is_never_expanded(repo: Path, posix: str) -> None:
    _source(repo, "src/a.py", "keep\n")
    _source(repo, "src/sub/b.py", "keep\n")
    plan = _constraints_plan('- note #7 - absent: "keep" in src/*.py')
    run = _constraints(repo, posix, plan)
    assert run.stdout.splitlines() == [
        "broken: note #7 src/a.py:1",
        "constraints: 0 hold, 1 broken",
    ]


@pytest.mark.parametrize(
    ("kind", "content", "expected", "code"),
    [
        ("absent", "a\nb\nsleep(1)\n", ["broken: note #7 src/a.py:3"], 1),
        ("absent", "a\nb\n", [], 0),
        ("present", "a\nb\nsleep(1)\n", [], 0),
        ("present", "a\nb\n", ["broken: note #7 (no match)"], 1),
    ],
)
def test_constraints_absent_and_present(
    repo: Path,
    posix: str,
    kind: str,
    content: str,
    expected: list[str],
    code: int,
) -> None:
    _source(repo, "src/a.py", content)
    run = _constraints(
        repo, posix, _constraints_plan(f'- note #7 - {kind}: "sleep(" in src/**')
    )
    assert run.returncode == code, run.stdout + run.stderr
    held, broken = (1, 0) if code == 0 else (0, 1)
    assert run.stdout.splitlines() == [
        *expected,
        f"constraints: {held} hold, {broken} broken",
    ]


def test_constraints_searches_untracked_files_but_not_ignored_ones(
    repo: Path, posix: str
) -> None:
    plan = _constraints_plan('- note #7 - absent: "secret" in **')
    _source(repo, "plans/ignored.txt", "secret\n")
    _source(repo, ".whygraph/x.txt", "secret\n")
    assert _constraints(repo, posix, plan).returncode == 0
    _source(repo, "new.txt", "secret\n")
    run = _constraints(repo, posix, plan)
    assert run.returncode == 1
    assert "broken: note #7 new.txt:1" in run.stdout.splitlines()


def test_constraints_reads_only_the_glob(repo: Path, posix: str) -> None:
    _source(repo, "docs/a.md", "forbidden\n")
    plan = _constraints_plan(
        '- note #7 - absent: "forbidden" in src/**',
        '- note #8 - absent: "forbidden" in docs/*.md',
        '- note #9 - present: "alpha" in *.txt',
    )
    run = _constraints(repo, posix, plan)
    assert run.returncode == 1
    assert run.stdout.splitlines() == [
        "broken: note #8 docs/a.md:1",
        "constraints: 2 hold, 1 broken",
    ]


def test_constraints_exempts_a_note_changed_on_purpose(repo: Path, posix: str) -> None:
    # Criterion 11: a step that names the note in Changes on purpose exempts it,
    # from that step on.
    _source(repo, "src/a.py", "a\nb\nsleep(1)\n")
    check = '- note #7 - absent: "sleep(" in src/**'
    plan = _constraints_plan(
        check, purpose={2: '"never sleep" (note #7) - the task needs a pause'}
    )
    first = _constraints(repo, posix, plan, "1")
    assert first.returncode == 1
    assert first.stdout.splitlines()[0] == "broken: note #7 src/a.py:3"
    for args in (("2",), ("3",)):
        run = _constraints(repo, posix, plan, *args)
        assert run.returncode == 0, run.stdout + run.stderr
        assert run.stdout.splitlines() == ["constraints: 0 hold, 0 broken"]
    # Without a step number nothing is exempt.
    assert _constraints(repo, posix, plan).returncode == 1


def test_constraints_exemption_names_only_its_note(repo: Path, posix: str) -> None:
    _source(repo, "src/a.py", "sleep(1)\n")
    plan = _constraints_plan(
        '- note #7 - absent: "sleep(" in src/**',
        '- note #17 - absent: "sleep(" in src/**',
        purpose={1: '"never sleep\n     at all" (note #17) - why'},
    )
    run = _constraints(repo, posix, plan, "1")
    assert run.returncode == 1
    assert run.stdout.splitlines() == [
        "broken: note #7 src/a.py:1",
        "constraints: 0 hold, 1 broken",
    ]


def test_constraints_a_later_step_does_not_exempt(repo: Path, posix: str) -> None:
    _source(repo, "src/a.py", "sleep(1)\n")
    plan = _constraints_plan(
        '- note #7 - absent: "sleep(" in src/**',
        purpose={3: '"never sleep" (note #7) - why'},
    )
    assert _constraints(repo, posix, plan, "2").returncode == 1


@pytest.mark.parametrize(
    "text",
    [
        _constraints_plan(),
        _constraints_plan().replace(
            "## Steps", "```\n## Constraint checks\n```\n\n## Steps"
        ),
    ],
)
def test_constraints_without_a_section_exits_6(
    repo: Path, posix: str, text: str
) -> None:
    run = _constraints(repo, posix, text)
    assert run.returncode == 6, run.stdout + run.stderr


def test_constraints_an_empty_section_holds(repo: Path, posix: str) -> None:
    plan = _constraints_plan().replace("## Steps", "## Constraint checks\n\n## Steps")
    run = _constraints(repo, posix, plan)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.splitlines() == ["constraints: 0 hold, 0 broken"]


@pytest.mark.parametrize(
    "line",
    [
        '- note #7 - absent: "a`touch pwned`" in **',
        '- note #7 - absent: "abc" in src/../../**',
        '- note #7 - absent: "abc" in /etc/**',
        '- note #7 - absent: "abc" in src/$(touch pwned)',
        "- note #7 - absent: $(touch pwned) in **",
    ],
)
def test_constraints_refuses_a_bad_line_before_any_search(
    repo: Path, posix: str, line: str
) -> None:
    # The plan file is never trusted to have passed check.
    plan = _constraints_plan('- note #1 - absent: "fine" in **', line)
    run = _constraints(repo, posix, plan)
    assert run.returncode == 5, run.stdout + run.stderr
    assert run.stdout == ""
    assert f"line {_line_of(plan, line)}: Constraint check" in run.stderr
    assert not (repo / "pwned").exists()


def test_constraints_reads_the_plan_from_a_blob(repo: Path, posix: str) -> None:
    saved = _constraints_plan('- note #7 - present: "nowhere" in **')
    blob = _git(repo, "hash-object", "-w", "--stdin", stdin=saved)
    run = _constraints(repo, posix, _constraints_plan(), blob, "1")
    assert run.returncode == 1, run.stdout + run.stderr
    assert run.stdout.splitlines()[0] == "broken: note #7 (no match)"


def test_constraints_a_blob_that_cannot_be_read_exits_65(
    repo: Path, posix: str
) -> None:
    run = _constraints(repo, posix, _constraints_plan(), "c" * 40)
    assert run.returncode == 65
    assert "the saved plan was changed" in run.stderr


def test_constraints_runs_from_the_repo_root(repo: Path, posix: str) -> None:
    _source(repo, "src/a.py", "sleep(1)\n")
    _write_plan(repo, _constraints_plan('- note #7 - absent: "sleep(" in src/**'))
    run = _tool(repo / "src", "constraints", "../plans/p.md", shell=_SHELLS[posix])
    assert run.returncode == 1, run.stdout + run.stderr
    assert run.stdout.splitlines()[0] == "broken: note #7 src/a.py:1"


# ---- brief (plan 54 section 5.15) -------------------------------------------------

_BRIEF_PLAN = """\
# Plan: Sort the export

**Status:** PROPOSAL - nothing implemented yet.
**Objective:** Sort the export by name.
**Non-goals:** none

## Working set
1 symbol analyzed.

```
## Steps
1. **A fenced step**
   - **Model:** haiku
```

## Blockers

- **Constraint at risk:** "Rows leave in id order." (note #7)
- **Why it's a blocker:** the task sorts by name

## Constraint checks
- note #7 - absent: "sorted(" in src/**

## Steps

1. **Sort the rows**
   - **Files:** src/export.py:10-20, src/sort.py (new, a helper)
   - **Change:** Sort the rows by name.
   - **Changes on purpose:** "Rows leave in id order." (note #7) - the task asks for it
   - **Model:** Sonnet - a small change
   - **Verify:** `touch ran`
2. **Show the order**
   - **Files:** src/page.py:3
   - **Model:** opus - new design
   - **Verify:** manual: look at the page
3. **Document it**
   - **Change:** Write it down.

## Risks called out across the change
- "a risk"

## Acceptance
1. The suite passes - **Check:** `touch ran`
2. The page shows the order
   - **Check:** manual: open the page

## Open questions
- **Q1.** Which order? **Options:** name / date **Default:** name **Plan-shaping:** no - one line

## Confidence: high
A reason.
"""

_BRIEF_PAGE = """\
Plan: Sort the export
Objective: Sort the export by name.
Blockers:
  - **Constraint at risk:** "Rows leave in id order." (note #7)
  - **Why it's a blocker:** the task sorts by name
Open questions:
  - **Q1.** Which order? **Options:** name / date **Default:** name **Plan-shaping:** no - one line
Constraint checks:
  - note #7 - absent: "sorted(" in src/**
Steps:
  1. Sort the rows - 2 files, sonnet, changes a constraint on purpose
  2. Show the order - 1 file, opus
  3. Document it - 0 files, no model
Acceptance:
  1. The suite passes - **Check:** `touch ran`
  2. The page shows the order
"""


def test_brief_prints_the_page_and_runs_nothing(repo: Path, posix: str) -> None:
    _write_plan(repo, _BRIEF_PLAN)
    run = _run(repo, posix, "brief", "plans/p.md")
    assert run.returncode == 0, run.stderr
    assert run.stdout == _BRIEF_PAGE
    assert not (repo / "ran").exists()
    assert not (repo / ".whygraph").exists()


def test_brief_prints_only_what_the_plan_has(tmp_path: Path) -> None:
    """Outside a repository too: brief reads the plan file and nothing else."""
    (tmp_path / "p.md").write_text(_steps_plan(("a.txt", "`true`")))
    run = _tool(tmp_path, "brief", "p.md")
    assert run.returncode == 0, run.stderr
    assert run.stdout == (
        "Plan: Test\nSteps:\n  1. Step 1 - 1 file, no model\n"
        "Acceptance:\n  1. Done - **Check:** `true`\n"
    )


@pytest.mark.parametrize(
    "args", [("brief",), ("brief", "missing.md"), ("brief", "p.md", "1")]
)
def test_brief_usage_errors_exit_64(tmp_path: Path, args: tuple[str, ...]) -> None:
    (tmp_path / "p.md").write_text(_BRIEF_PLAN)
    assert _tool(tmp_path, *args).returncode == 64


# ---- refusals -------------------------------------------------------------------

_T0_ARG = "<t0>"
_BLOB_ARG = "<blob>"
# Every command of the plan and of its Check would create "ran".
_REFUSAL_PLAN = _full_plan(("ran", "`touch ran`", "none"), checks=("`touch ran`",))
_PLAN_COMMANDS = [
    ("verify", "plans/p.md", "1"),
    ("begin", "plans/p.md", "1"),
    ("end", "plans/p.md", "1", _T0_ARG),
    ("pass", "plans/p.md", "1"),
    ("next", "plans/p.md"),
    ("probe", "plans/p.md"),
    ("baseline", "plans/p.md", "1"),
    ("accept", "plans/p.md", "1"),
    ("attempt", "plans/p.md", "1", "retry", "sonnet:opus"),
    ("amends", "plans/p.md", "1"),
    ("save", "plans/p.md"),
    ("restore", "plans/p.md", _BLOB_ARG),
    ("log-check", "plans/p.md", _BLOB_ARG),
    ("state", "plans/p.md"),
    ("amend-check", "plans/p.md", _BLOB_ARG, "1"),
]


def _refused(repo: Path, posix: str, args: tuple[str, ...]) -> None:
    """Run ``args`` and check the refusal: exit 77, no command run, no write."""
    tree = _snapshot(repo)
    plan = (repo / "plans" / "p.md").read_text()
    blob = _git(repo, "hash-object", "-w", "--stdin", stdin="other\n")
    swap = {_T0_ARG: tree, _BLOB_ARG: blob}
    run = _run(repo, posix, *[swap.get(a, a) for a in args])
    assert run.returncode == 77, run.stdout + run.stderr
    assert "refused" in run.stderr
    assert not (repo / "ran").exists()
    assert (repo / "plans" / "p.md").read_text() == plan


@pytest.mark.parametrize("args", _PLAN_COMMANDS, ids=lambda a: a[0])
def test_refuses_a_tracked_steps_file(
    repo: Path, posix: str, args: tuple[str, ...]
) -> None:
    _write_plan(repo, _REFUSAL_PLAN)
    _steps_file(repo, "1.t1", f"{_snapshot(repo)}\n")
    _git(repo, "add", "-f", str(_STEPS / "1.t1"))
    _refused(repo, posix, args)


@pytest.mark.parametrize("args", _PLAN_COMMANDS, ids=lambda a: a[0])
@pytest.mark.parametrize(
    "link", [".whygraph", ".whygraph/plans", ".whygraph/plans/p", str(_STEPS)]
)
def test_refuses_a_symlinked_directory(
    repo: Path, posix: str, args: tuple[str, ...], link: str
) -> None:
    _write_plan(repo, _REFUSAL_PLAN)
    target = repo.parent / "elsewhere"
    target.mkdir()
    (repo / link).parent.mkdir(parents=True, exist_ok=True)
    (repo / link).symlink_to(target)
    _refused(repo, posix, args)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize(
    ("args", "leaf"),
    [
        (("begin", "plans/p.md", "1"), "attempts"),
        (("begin", "plans/p.md", "1"), "1.t0"),
        (("verify", "plans/p.md", "1"), "1.verify"),
        (("end", "plans/p.md", "1", _T0_ARG), "1.verify"),
        (("end", "plans/p.md", "1", _T0_ARG), "1.t1"),
        (("end", "plans/p.md", "1", _T0_ARG), "1.diff"),
        (("pass", "plans/p.md", "1"), "1.done"),
        (("baseline", "plans/p.md", "1"), "baseline"),
        (("baseline", "plans/p.md", "1", "first"), "baseline"),
        (("accept", "plans/p.md", "1"), "acceptance.1"),
        (("attempt", "plans/p.md", "1", "constraint"), "attempts"),
        (("attempt", "plans/p.md", "1", "reset"), "attempts"),
    ],
    ids=lambda v: v if isinstance(v, str) else v[0],
)
def test_refuses_a_symlinked_file(
    repo: Path, posix: str, args: tuple[str, ...], leaf: str
) -> None:
    _write_plan(repo, _REFUSAL_PLAN)
    _steps_file(repo, "1.t1", f"{_snapshot(repo)}\n")
    target = repo.parent / "target.txt"
    target.write_text("keep\n")
    path = repo / _STEPS / leaf
    path.unlink(missing_ok=True)
    path.symlink_to(target)
    _refused(repo, posix, args)
    assert path.is_symlink()
    assert target.read_text() == "keep\n"


# ---- guard.sh ---------------------------------------------------------------


_FLAGS = "-U0 --inter-hunk-context=3 --no-color --no-ext-diff"
_T0 = "a" * 40
_T1 = "b" * 40
_CHECK = "sh .claude/skills/whygraph-plan/plan-tool.sh check"
# Not JSON, or JSON the extraction cannot read: the fail-closed modes block.
_MALFORMED = ("", "not json", '{"tool_input":{"command":"git diff HEAD')


def _hook_input(
    tool: str,
    tool_input: dict[str, object],
    separators: tuple[str, str] = (",", ":"),
) -> str:
    """Return the JSON Claude Code sends a ``PreToolUse`` hook on stdin."""
    return json.dumps(
        {
            "session_id": "s",
            "transcript_path": "/home/u/.claude/projects/p/s.jsonl",
            "cwd": "/repo",
            "permission_mode": "default",
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
            "tool_use_id": "toolu_1",
        },
        separators=separators,
    )


def _bash(command: str, separators: tuple[str, str] = (",", ":")) -> str:
    return _hook_input("Bash", {"command": command, "description": "run"}, separators)


def _guard(
    stdin: str, *args: str, project_dir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = _env()
    env.pop("CLAUDE_PROJECT_DIR", None)
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    return subprocess.run(
        ["sh", str(_GUARD), *args],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _assert_allowed(run: subprocess.CompletedProcess[str]) -> None:
    assert run.returncode == 0, run.stderr
    assert run.stderr == ""
    assert run.stdout == ""


def _assert_blocked(run: subprocess.CompletedProcess[str]) -> None:
    assert run.returncode == 2, run.stderr
    assert run.stderr.count("\n") == 1, run.stderr
    assert run.stderr.startswith("guard.sh: ")
    assert run.stdout == ""


@pytest.mark.parametrize("args", [(), ("nope",), ("Reviewer",), ("",)])
def test_guard_blocks_an_unknown_mode(args: tuple[str, ...]) -> None:
    _assert_blocked(_guard(_bash("git diff HEAD"), *args))


@pytest.mark.parametrize(
    "command",
    [
        f"git diff {_FLAGS} {_T0} {_T1}",
        f"git diff {_FLAGS} --merge-base origin/main",
        "git diff HEAD --stat",
        f"git diff {_FLAGS} HEAD",
        f"git diff {_FLAGS} --merge-base feature/a.b_c^~1",
    ],
)
def test_guard_reviewer_allows_the_review_diffs(command: str) -> None:
    _assert_allowed(_guard(_bash(command), "reviewer"))


def test_guard_reviewer_reads_spaced_json() -> None:
    stdin = _bash("git diff HEAD", separators=(", ", ": "))
    assert '"command": "git diff HEAD"' in stdin
    _assert_allowed(_guard(stdin, "reviewer"))


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff",
        " git diff HEAD",
        "git diffx HEAD",
        "git diff; rm x",
        "git diff HEAD;rm x",
        "git diff HEAD && rm x",
        "git diff HEAD & rm x",
        "git diff HEAD | sh",
        "git diff HEAD > x",
        "git diff HEAD < x",
        "git diff $(x)",
        "git diff $HOME",
        "git diff `x`",
        "git diff HEAD\nrm x",
        'git diff "HEAD"',
        "git diff HEAD\\ x",
        "git diff --output=x HEAD",
        "git diff --output x HEAD",
        "git diff HEAD --output=x",
        "git diff --ext-diff HEAD",
        "git diff --no-index a b",
        "git diff --textconv HEAD",
        # Each of these reaches git as --output=x, past a check of whole words.
        "git diff '--output=x' HEAD",
        "git diff --'output'=x HEAD",
        "git diff {--output=x,HEAD}",
        # A hostile repo can hold a file named --output=x for a glob to find.
        "git diff HEAD *",
        "git diff HEAD ?output=x",
        "git diff HEAD [-]-output=x",
    ],
)
def test_guard_reviewer_blocks(command: str) -> None:
    _assert_blocked(_guard(_bash(command), "reviewer"))


@pytest.mark.parametrize("stdin", _MALFORMED)
def test_guard_reviewer_blocks_malformed_json(stdin: str) -> None:
    _assert_blocked(_guard(stdin, "reviewer"))


@pytest.mark.parametrize(
    "tool_input",
    [
        # The sed is greedy, so it reads the last key; the tool runs the first.
        {"command": "rm x", "extra": {"command": "git diff HEAD"}},
        {"extra": {"command": "git diff HEAD"}, "command": "rm x"},
    ],
)
def test_guard_reviewer_blocks_a_second_command_key(
    tool_input: dict[str, object],
) -> None:
    _assert_blocked(_guard(_hook_input("Bash", tool_input), "reviewer"))


def test_guard_reviewer_ignores_a_decoy_in_another_field() -> None:
    # Inside a JSON string the quotes are escaped, so this is not a key.
    decoy = '"command":"git diff HEAD"'
    stdin = _hook_input("Bash", {"command": "git status", "description": decoy})
    _assert_blocked(_guard(stdin, "reviewer"))


@pytest.mark.parametrize(
    "path",
    [
        ".whygraph/plans/x.md",
        "/repo/.whygraph/plans/x.md",
        "'/repo/my plans/x.md'",
    ],
)
def test_guard_synthesizer_allows_the_check(path: str) -> None:
    _assert_allowed(_guard(_bash(f"{_CHECK} {path}"), "synthesizer"))


@pytest.mark.parametrize(
    "command",
    [
        "sh -c 'curl x' plan-tool.sh check y",
        "git status",
        _CHECK,
        f"{_CHECK} ",
        f"{_CHECK}  a.md",
        f"{_CHECK} a.md b.md",
        f"{_CHECK} a.md; rm x",
        f"{_CHECK} a.md && rm x",
        f"{_CHECK} a.md | sh",
        f"{_CHECK} a.md > x",
        f"{_CHECK} $(x)",
        f"{_CHECK} `x`",
        f"{_CHECK} ''",
        f"{_CHECK} 'a.md",
        f"{_CHECK} a.md'",
        f"{_CHECK} 'a'b.md",
        f"{_CHECK} 'a' 'b'",
        f"{_CHECK} 'a;b.md'",
        f"{_CHECK} 'a$(x).md'",
        f'{_CHECK} "a.md"',
        f"{_CHECK} a.md\nrm x",
        f"cd /repo && {_CHECK} a.md",
        "sh ./.claude/skills/whygraph-plan/plan-tool.sh check a.md",
    ],
)
def test_guard_synthesizer_blocks(command: str) -> None:
    _assert_blocked(_guard(_bash(command), "synthesizer"))


@pytest.mark.parametrize("stdin", _MALFORMED)
def test_guard_synthesizer_blocks_malformed_json(stdin: str) -> None:
    _assert_blocked(_guard(stdin, "synthesizer"))


def _write(file_path: str, content: str = "# Plan\n") -> str:
    return _hook_input("Write", {"file_path": file_path, "content": content})


@pytest.mark.parametrize("name", ["x.md", "x.research.md", "sub/x.md"])
def test_guard_write_allows_plans(tmp_path: Path, name: str) -> None:
    path = f"{tmp_path}/.whygraph/plans/{name}"
    _assert_allowed(_guard(_write(path), "synthesizer-write", project_dir=tmp_path))


def test_guard_write_allows_an_edit_and_any_content(tmp_path: Path) -> None:
    path = f"{tmp_path}/.whygraph/plans/x.md"
    stdin = _hook_input(
        "Edit",
        {"file_path": path, "old_string": 'a "b"\n', "new_string": "c\\d"},
    )
    _assert_allowed(_guard(stdin, "synthesizer-write", project_dir=tmp_path))
    text = 'see "file_path": "/etc/passwd" and "command": "x"\n'
    _assert_allowed(
        _guard(_write(path, text), "synthesizer-write", project_dir=tmp_path)
    )


@pytest.mark.parametrize(
    "rel",
    [
        "CLAUDE.md",
        "AGENTS.md",
        ".claude/agents/x.md",
        ".whygraph/plans/../../x.md",
        ".whygraph/plans/..",
        "a.py",
        ".whygraph/plans/x.txt",
        ".whygraph/plansx/y.md",
        ".whygraph/plans/x.md\n",
    ],
)
def test_guard_write_blocks(tmp_path: Path, rel: str) -> None:
    run = _guard(_write(f"{tmp_path}/{rel}"), "synthesizer-write", project_dir=tmp_path)
    _assert_blocked(run)


def test_guard_write_blocks_a_path_outside_the_project(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    for path in (
        f"{tmp_path}/other/.whygraph/plans/x.md",
        ".whygraph/plans/x.md",
        "/etc/passwd",
    ):
        _assert_blocked(_guard(_write(path), "synthesizer-write", project_dir=project))


def test_guard_write_blocks_a_decoy_in_the_content(tmp_path: Path) -> None:
    decoy = f'"file_path":"{tmp_path}/.whygraph/plans/x.md"'
    run = _guard(
        _write(f"{tmp_path}/CLAUDE.md", decoy),
        "synthesizer-write",
        project_dir=tmp_path,
    )
    _assert_blocked(run)


@pytest.mark.parametrize("rel", ["plans/x.md", "docs/plan.md", "x.md"])
def test_guard_write_blocks_the_review_fixer_outside_plans(
    tmp_path: Path, rel: str
) -> None:
    # The plan review's fixer is the synthesizer. With --out outside
    # .whygraph/plans/ its write is refused, and it returns the plan instead.
    for tool in ("Write", "Edit"):
        stdin = _hook_input(tool, {"file_path": f"{tmp_path}/{rel}", "content": "x"})
        _assert_blocked(_guard(stdin, "synthesizer-write", project_dir=tmp_path))


def test_guard_write_blocks_without_a_project_dir() -> None:
    _assert_blocked(_guard(_write("/.whygraph/plans/x.md"), "synthesizer-write"))


@pytest.mark.parametrize("stdin", _MALFORMED)
def test_guard_write_blocks_malformed_json(tmp_path: Path, stdin: str) -> None:
    _assert_blocked(_guard(stdin, "synthesizer-write", project_dir=tmp_path))


def _edit(file_path: str) -> str:
    return _hook_input(
        "Edit",
        {"file_path": file_path, "old_string": "a", "new_string": 'b "c"\\d'},
    )


@pytest.mark.parametrize(
    "rel",
    [
        ".whygraph/plans/x.md",
        "plans/50-x.md",
        "x.md",
        "docs/a/b.md",
        "github/x.md",
        ".gitignore.md",
    ],
)
def test_guard_amender_write_allows_markdown(tmp_path: Path, rel: str) -> None:
    run = _guard(_edit(f"{tmp_path}/{rel}"), "amender-write", project_dir=tmp_path)
    _assert_allowed(run)


@pytest.mark.parametrize(
    "rel",
    [
        "a.py",
        "x.md.txt",
        "x.MD",
        ".whygraph/plans/x.txt",
        ".whygraph/plans/../../x.md",
        "../x.md",
        "a/../b.md",
        ".git/x.md",
        ".git/hooks/x.md",
        "sub/.git/x.md",
        ".claude/agents/x.md",
        ".cursor/agents/x.md",
        ".github/x.md",
        ".codex/agents/x.md",
        "./.claude/x.md",
        "a//.git/x.md",
        ".Claude/agents/x.md",
        ".GIT/x.md",
        "x.md\n",
    ],
)
def test_guard_amender_write_blocks(tmp_path: Path, rel: str) -> None:
    run = _guard(_edit(f"{tmp_path}/{rel}"), "amender-write", project_dir=tmp_path)
    _assert_blocked(run)


def test_guard_amender_write_blocks_a_path_outside_the_project(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    for path in (f"{tmp_path}/other/x.md", f"{project}x/x.md", "x.md", "/etc/x.md"):
        _assert_blocked(_guard(_edit(path), "amender-write", project_dir=project))


def test_guard_amender_write_blocks_without_a_project_dir() -> None:
    _assert_blocked(_guard(_edit("/x.md"), "amender-write"))


def test_guard_amender_write_blocks_a_second_file_path_key(tmp_path: Path) -> None:
    tool_input = {
        "file_path": f"{tmp_path}/.claude/agents/x.md",
        "extra": {"file_path": f"{tmp_path}/x.md"},
    }
    stdin = _hook_input("Edit", tool_input)
    _assert_blocked(_guard(stdin, "amender-write", project_dir=tmp_path))


@pytest.mark.parametrize("stdin", _MALFORMED)
def test_guard_amender_write_blocks_malformed_json(tmp_path: Path, stdin: str) -> None:
    _assert_blocked(_guard(stdin, "amender-write", project_dir=tmp_path))


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push origin main",
        "git -C . push",
        "git -c a=b push origin main",
        "git -C sub -c a=b push",
        "git --no-pager push",
        "cd x && git push",
        "cd x; git push",
        "cd x;git push",
        "false || git push",
        "true | git push",
        "sleep 1 & git push",
        'git commit -m "x" && git push',
        "git status\ngit push",
        "   git push",
    ],
)
def test_guard_implementor_blocks_a_push(command: str) -> None:
    _assert_blocked(_guard(_bash(command), "implementor"))


@pytest.mark.parametrize(
    "command",
    [
        "git stash push",
        "git stash push -m x",
        "git -C . stash push",
        "grep push f",
        "echo git push",
        "git pushx",
        "git status",
        "uv run pytest && git diff",
        'git commit -m "x"',
        "git status\ngit diff",
    ],
)
def test_guard_implementor_allows(command: str) -> None:
    _assert_allowed(_guard(_bash(command), "implementor"))


@pytest.mark.parametrize("stdin", _MALFORMED)
def test_guard_implementor_allows_malformed_json(stdin: str) -> None:
    _assert_allowed(_guard(stdin, "implementor"))


def test_guard_implementor_falls_open_on_another_json_escape() -> None:
    # Only \" and \n are normalized; any other escape is a parse fault (5.8).
    _assert_allowed(_guard(_bash("printf 'a\tb' && git push"), "implementor"))
