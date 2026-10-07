"""The LLM call-site inventory (M2f-2 plan section 6.3 #6).

Every provider round trip must be recorded in the usage ledger. This AST
scan of ``src/whygraph`` fails when a new call appears that the ledger
would miss:

(a) every ``.complete(`` / ``.stream_turn(`` call sits in one of the
    allowlisted **metered functions**, and each such call is followed by a
    ``record_usage(`` call before the next provider call and before anything
    parses the answer;
(b) raw provider SDK calls (``messages.create`` / ``messages.stream`` /
    ``chat.completions.create`` / Ollama's ``_client.chat``) appear only in
    the adapters under ``services/llm/``;
(c) the client ports expose no public method beyond the two metered ones
    and an explicit list of unmetered ones (``preflight``, ``list_models``,
    ``from_config``), so a new spending method cannot slip past (a).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import whygraph
from whygraph.services.llm import LlmClient
from whygraph.services.llm.chat import ChatClient

SRC = Path(whygraph.__file__).resolve().parent

METERED_CALLS = frozenset({"complete", "stream_turn"})
"""Client methods that make a provider round trip."""

METERED_FUNCTIONS = frozenset(
    {
        ("analyze/llm_descriptor.py", "LlmDescriptor._describe_one"),
        ("analyze/llm_descriptor.py", "LlmDescriptor._synthesize"),
        ("analyze/rationale_generator.py", "RationaleGenerator.generate"),
        ("chat/harness.py", "run_turn"),
    }
)
"""Where a metered call may live; each records it with ``record_usage``."""

UNMETERED_METHODS = frozenset({"preflight", "list_models", "from_config"})
"""Public client methods that never call a provider for tokens."""

ADAPTER_DIR = "services/llm/"


def _modules() -> list[tuple[str, ast.Module]]:
    return [
        (path.relative_to(SRC).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(SRC.rglob("*.py"))
    ]


def _functions(tree: ast.Module):  # noqa: ANN202 -- yields (qualname, node)
    """Every function in ``tree`` with its ``Class.method`` qualified name."""

    def walk(node: ast.AST, prefix: str):  # noqa: ANN202
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                yield from walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield f"{prefix}{child.name}", child
                yield from walk(child, f"{prefix}{child.name}.")

    yield from walk(tree, "")


def _calls(node: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def _name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _owner(tree: ast.Module, call: ast.Call) -> str | None:
    """The innermost function holding ``call``, by qualified name."""
    best: tuple[int, str] | None = None
    for qualname, fn in _functions(tree):
        end = fn.end_lineno or fn.lineno
        if fn.lineno <= call.lineno <= end:
            if best is None or fn.lineno >= best[0]:
                best = (fn.lineno, qualname)
    return None if best is None else best[1]


def test_every_metered_call_is_in_a_metered_function() -> None:
    found: set[tuple[str, str]] = set()
    stray: list[str] = []
    for rel, tree in _modules():
        if rel.startswith(ADAPTER_DIR):
            continue  # the adapters implement the ports; they do not call them
        for call in _calls(tree):
            if isinstance(call.func, ast.Attribute) and call.func.attr in METERED_CALLS:
                owner = _owner(tree, call)
                if (rel, owner) in METERED_FUNCTIONS:
                    found.add((rel, owner))  # type: ignore[arg-type]
                else:
                    stray.append(f"{rel}:{call.lineno} in {owner}")
    assert stray == [], f"unmetered provider calls: {stray}"
    assert found == METERED_FUNCTIONS  # the allowlist has no dead entries


def test_each_metered_call_is_recorded_before_any_parse() -> None:
    problems: list[str] = []
    for rel, tree in _modules():
        for qualname, fn in _functions(tree):
            if (rel, qualname) not in METERED_FUNCTIONS:
                continue
            calls = sorted(_calls(fn), key=lambda c: (c.lineno, c.col_offset))
            provider = [
                c
                for c in calls
                if isinstance(c.func, ast.Attribute) and c.func.attr in METERED_CALLS
            ]
            records = [c.lineno for c in calls if _name(c) == "record_usage"]
            parses = [c.lineno for c in calls if "parse" in (_name(c) or "")]
            for i, call in enumerate(provider):
                nxt = provider[i + 1].lineno if i + 1 < len(provider) else 10**9
                after = [line for line in records if call.lineno < line < nxt]
                if not after:
                    problems.append(f"{rel}:{call.lineno} ({qualname}) never recorded")
                    continue
                late = [p for p in parses if call.lineno < p < after[0]]
                if late:
                    problems.append(f"{rel}:{call.lineno} ({qualname}) parsed first")
    assert problems == []


def _is_raw_sdk_call(call: ast.Call) -> bool:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    inner = func.value
    if func.attr in ("create", "stream") and isinstance(inner, ast.Attribute):
        return inner.attr in ("messages", "completions")
    if func.attr == "chat" and isinstance(inner, ast.Attribute):
        return inner.attr == "_client"  # Ollama's client.chat(...)
    return False


def test_raw_sdk_calls_live_only_in_the_adapters() -> None:
    outside = [
        f"{rel}:{call.lineno}"
        for rel, tree in _modules()
        if not rel.startswith(ADAPTER_DIR)
        for call in _calls(tree)
        if _is_raw_sdk_call(call)
    ]
    assert outside == []
    inside = [
        rel
        for rel, tree in _modules()
        if rel.startswith(ADAPTER_DIR)
        for call in _calls(tree)
        if _is_raw_sdk_call(call)
    ]
    assert inside, "the scan no longer recognizes the adapters' SDK calls"


def test_client_ports_have_no_unlisted_methods() -> None:
    for port, metered in ((LlmClient, "complete"), (ChatClient, "stream_turn")):
        public = {
            name
            for name, member in inspect.getmembers(port)
            if not name.startswith("_") and callable(member)
        }
        assert public - UNMETERED_METHODS == {metered}, port
