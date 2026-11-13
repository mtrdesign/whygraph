"""Ratchet the prompt style counters against a committed baseline."""

from __future__ import annotations

import json
import re

import pytest
from prompt_style import (
    BASELINE,
    MAX_WORDS,
    NON_WORD,
    PHRASES,
    ROOT,
    RULES,
    counts,
    findings,
    sources,
)

_COMMAND = "uv run python tests/prompt_style.py --write-baseline"
_PLAN_TOOL = ROOT / "src/whygraph/assets/claude-code/skills/whygraph-plan/plan-tool.sh"


def _baseline() -> dict[str, dict[str, int]]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _listing(text: str, rule: str) -> list[tuple[int, str]]:
    return [f for f in findings(text) if f[1] == rule]


def test_prompt_style_does_not_rise() -> None:
    """No source has more findings of a rule than its baseline."""
    base = _baseline()
    texts = sources()
    problems = []
    for source_id, per_rule in counts().items():
        for rule in RULES:
            now = per_rule.get(rule, 0)
            if now > base.get(source_id, {}).get(rule, 0):
                problems.append(
                    f"{source_id}: {rule} {now} > "
                    f"{base.get(source_id, {}).get(rule, 0)}; "
                    f"findings {_listing(texts[source_id], rule)}"
                )
    assert not problems, (
        "\n".join(problems) + f"\nIf the rise is intended, run `{_COMMAND}`. "
        "The baseline diff shows it in review."
    )


def test_prompt_style_baseline_is_tight() -> None:
    """No source has fewer findings of a rule than its baseline."""
    now = counts()
    problems = []
    for source_id, per_rule in _baseline().items():
        for rule, expected in per_rule.items():
            got = now.get(source_id, {}).get(rule, 0)
            if got < expected:
                problems.append(f"{source_id}: {rule} {got} < {expected}")
    assert not problems, (
        "\n".join(problems) + f"\nRun `{_COMMAND}` to lock in the fall. "
        "The baseline diff shows it in review."
    )


def test_counter_constants_match_plan_tool() -> None:
    """The counter shares its limit, phrases and non-word class with the awk."""
    text = _PLAN_TOOL.read_text(encoding="utf-8")
    limit = re.search(r"^\s*MAX_WORDS = (\d+)\s*$", text, re.M)
    phrases = re.search(r'PHRASE_COUNT = split\("([^"]*)", PHRASE, ";"\)', text)
    non_word = re.search(r'PUNCTUATION_ONLY = "([^"]*)"', text)
    assert limit and phrases and non_word
    assert int(limit.group(1)) == MAX_WORDS
    assert tuple(phrases.group(1).split(";")) == PHRASES
    assert non_word.group(1) == NON_WORD.pattern


def _words(n: int) -> str:
    return " ".join(["word"] * n)


CASES = [
    ("over", _words(20) + " end.", [(1, "long-sentence")]),
    ("exact", _words(19) + " end.", []),
    ("two-lines", _words(11) + "\n" + _words(10) + ".", [(1, "long-sentence")]),
    ("label", "**Label.** " + _words(18) + ".", []),
    ("em-dash-word", _words(19) + " — end.", [(1, "dash"), (1, "long-sentence")]),
    ("and-then-twice", "do a and then b and then c.", [(1, "two-actions")]),
    ("and-also", "do a and also b.", [(1, "two-actions")]),
    ("comma-then", "do a, then b.", [(1, "two-actions")]),
    ("semicolon-end", "Do a;\nDo b.", [(1, "semicolon")]),
    (
        "clean",
        "```\na; b — c\n```\n\n| a; b — |\n\nUse `" + _words(30) + " ; —` now.",
        [],
    ),
]


@pytest.mark.parametrize(("name", "text", "expected"), CASES, ids=[c[0] for c in CASES])
def test_counter_rules(name: str, text: str, expected: list[tuple[int, str]]) -> None:
    """Each counting rule gives the plan's result on a small case."""
    assert sorted(findings(text)) == sorted(expected)
