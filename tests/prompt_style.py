"""Count simplified-technical-English style findings in WhyGraph's prompts.

Not collected by pytest; import it as ``from prompt_style import ...``.
The counter backs ``tests/test_prompt_style.py``, a ratchet against
``tests/fixtures/prompt_style_baseline.json``. The limit, phrases and
non-word class equal the ones in the claude-code ``plan-tool.sh``.

Rewrite the baseline with::

    uv run python tests/prompt_style.py --write-baseline
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

RULES = ("long-sentence", "two-actions", "semicolon", "dash")
MAX_WORDS = 20  # = plan-tool.sh:397
PHRASES = (" and then ", " and also ", ", then ")  # = plan-tool.sh:412
NON_WORD = re.compile(r"^[-/+=&|*.?!;,:]+$")  # = plan-tool.sh:401, verbatim
ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tests" / "fixtures" / "prompt_style_baseline.json"

_FENCE = re.compile(r"^\s*(```|~~~)")
_CODE = re.compile(r"`[^`\n]+`")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+\.)\s")
_DASHES = ("—", "–")
_TRAILING = "*_)\"']"


def _skipped_lines(lines: list[str]) -> set[int]:
    """Return the 0-based indexes of lines that are not prose."""
    skip: set[int] = set()
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                skip.update(range(0, i + 1))
                start = i + 1
                break
    fence = None
    in_comment = False
    for i in range(start, len(lines)):
        line = lines[i]
        stripped = line.strip()
        if fence is not None:
            skip.add(i)
            if stripped.startswith(fence):
                fence = None
            continue
        match = _FENCE.match(line)
        if match:
            fence = match.group(1)
            skip.add(i)
            continue
        if in_comment:
            skip.add(i)
            if "-->" in line:
                in_comment = False
            continue
        if stripped.startswith("<!--"):
            skip.add(i)
            if "-->" not in line:
                in_comment = True
            continue
        if stripped.startswith("#") or stripped.startswith("|"):
            skip.add(i)
    return skip


def findings(text: str) -> list[tuple[int, str]]:
    """Return ``(line, rule)`` findings for one prompt text.

    Parameters
    ----------
    text : str
        The prompt text. Line numbers count from 1 at its first line.

    Returns
    -------
    list of tuple of (int, str)
        One entry per finding, in line order.
    """
    lines = text.split("\n")
    skip = _skipped_lines(lines)
    out: list[tuple[int, str]] = []
    words = 0
    first = 0

    def end_sentence() -> None:
        nonlocal words
        if words > MAX_WORDS:
            out.append((first, "long-sentence"))
        words = 0

    for i, raw in enumerate(lines):
        number = i + 1
        if i in skip:
            end_sentence()
            continue
        line = _CODE.sub("x", raw)
        if not line.strip():
            end_sentence()
            continue
        if _LIST_ITEM.match(line):
            end_sentence()
        for _ in range(line.count(";")):
            out.append((number, "semicolon"))
        for dash in _DASHES:
            for _ in range(line.count(dash)):
                out.append((number, "dash"))
        padded = line + " "
        for phrase in PHRASES:
            if phrase in padded:
                out.append((number, "two-actions"))
        for token in line.split():
            if NON_WORD.match(token):
                continue
            if words == 0:
                first = number
            words += 1
            if token.rstrip(_TRAILING)[-1:] in (".", "?", "!", ";"):
                end_sentence()
    end_sentence()
    return sorted(out, key=lambda item: item[0])


def _tool_text(spec: object) -> str:
    """Join a tool's description with every nested parameter description."""
    parts = [spec.description]  # type: ignore[attr-defined]

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "description" and isinstance(value, str):
                    parts.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(spec.parameters)  # type: ignore[attr-defined]
    return "\n\n".join(parts)


def sources() -> dict[str, str]:
    """Return every prompt text to count, keyed by source id."""
    out: dict[str, str] = {}
    assets = ROOT / "src" / "whygraph" / "assets"
    for path in sorted(assets.rglob("*")):
        rel = path.relative_to(assets).as_posix()
        if path.suffix in (".md", ".mdc"):
            out[f"assets/{rel}"] = path.read_text(encoding="utf-8")
        elif path.suffix == ".toml":
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            if "developer_instructions" in data:
                out[f"assets/{rel}"] = data["developer_instructions"]
    for name in ("chat", "analyze"):
        base = ROOT / "src" / "whygraph" / name / "prompts"
        for path in sorted(base.rglob("*.md")):
            rel = path.relative_to(base).as_posix()
            out[f"{name}/prompts/{rel}"] = path.read_text(encoding="utf-8")
    from whygraph.mcp import descriptions

    for key, value in vars(descriptions).items():
        if key.isupper() and isinstance(value, str):
            out[f"mcp.descriptions.{key}"] = value
    from whygraph.chat.tools import TOOL_SPECS

    for spec in TOOL_SPECS:
        out[f"chat.tools.{spec.name}"] = _tool_text(spec)
    return out


def counts() -> dict[str, dict[str, int]]:
    """Return per-source rule counts. Zero counts and clean sources are omitted."""
    result: dict[str, dict[str, int]] = {}
    for source_id, text in sources().items():
        per_rule: dict[str, int] = {}
        for _line, rule in findings(text):
            per_rule[rule] = per_rule.get(rule, 0) + 1
        if per_rule:
            result[source_id] = per_rule
    return result


def render(data: dict[str, dict[str, int]]) -> str:
    """Return the baseline JSON text for ``data``."""
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    if "--write-baseline" not in sys.argv[1:]:
        sys.exit("usage: python tests/prompt_style.py --write-baseline")
    sys.path.insert(0, str(ROOT / "src"))
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(render(counts()), encoding="utf-8")
    print(f"wrote {BASELINE.relative_to(ROOT)}")
