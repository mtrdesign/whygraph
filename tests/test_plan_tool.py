"""Tests for ``plan-tool.sh``, the deterministic half of the planning harness.

The script is POSIX sh and POSIX awk, so these tests run it through ``sh`` with
the host's own ``awk`` and ``git`` (BSD awk on macOS, mawk or gawk in CI). The
byte identity of its four copies is pinned in ``tests/test_assets.py``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
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


def _tool(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(_SCRIPT), *args],
        cwd=cwd,
        env=_env(),
        capture_output=True,
        text=True,
        timeout=60,
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
        "# Plan: x\n\n1. **Only**\n   - **Change:** y\n"
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
    text = "1. **One**\n   - **Change:** a\n2. **Two**\n   - **Verify:** `touch ran`\n"
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


def test_guard_write_blocks_without_a_project_dir() -> None:
    _assert_blocked(_guard(_write("/.whygraph/plans/x.md"), "synthesizer-write"))


@pytest.mark.parametrize("stdin", _MALFORMED)
def test_guard_write_blocks_malformed_json(tmp_path: Path, stdin: str) -> None:
    _assert_blocked(_guard(stdin, "synthesizer-write", project_dir=tmp_path))


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
