"""The error-code inventory: every code the backend can return is in the SPA's registry.

``src/playground/src/lib/api-error-codes.json`` lists every machine-readable
``code`` an API answer (or a chat ``error`` frame) can carry; the
playground's ``lib/apiErrors.ts`` holds a human message for each, and
``test/apiErrors.test.ts`` checks the two agree. This test builds the same
set from the backend by an AST scan of ``src/whygraph/``, so a new code
fails CI until the JSON and the registry have it (M2f-3 plan section 4.3):

(a) every ``code=`` keyword whose value is a string literal, in a call to
    one of :data:`ERROR_CALLS` (``Name(...)`` or ``Name.wrap(...)``);
(b) every dict literal with a ``"code"`` key whose value is a string
    literal (a hand-built body, a chat SSE frame);
(c) every ``code=`` / ``"code":`` of rule (a) / (b) whose value is the
    name of a module-level ``str`` constant, resolved to its value;
(d) the literals ``serve/chat.py``'s ``_sse_error_code`` returns;

plus :data:`VARIABLE_CODES`, the codes built from variables the scan
cannot follow.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "whygraph"
CODES_JSON = ROOT / "src" / "playground" / "src" / "lib" / "api-error-codes.json"

ERROR_CALLS = frozenset(
    {
        "ApiError",
        "ServeError",
        "WhyGraphError",
        "RepoAccessError",
        # WhyGraphError subclasses raised with an explicit code.
        "NoEvidenceError",
        "RationaleGenerationError",
        "GenerationNotPermitted",
    }
)
"""The exception types whose ``code=`` keyword is an API error code."""

VARIABLE_CODES = frozenset(
    {
        # `exc.reason` of a UsageBlocked (portal/app.py `_usage_blocked_handler`).
        "budget_exceeded",
        # `refusal.code` of a refused connection token (portal/deps.py `v1_refusal`).
        "invalid_token",
        "token_revoked",
        "throttled",
        # `_bad(message, code)` in portal/usage_routes.py.
        "bad_cursor",
        "bad_filter",
        "bad_group",
        "bad_range",
        "bad_sort",
        "range_too_long",
        # RepoAccessError's positional codes, re-raised as `code=exc.code`
        # (portal/routes.py `_probe`).
        "bad_token",
        "no_access",
        "not_found",
    }
)
"""Codes the backend builds from a variable, which the AST scan cannot see."""


def _call_name(func: ast.expr) -> str | None:
    """``Name`` for ``Name(...)`` and ``Name.wrap(...)``; else ``None``."""
    if isinstance(func, ast.Name):
        return func.id
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "wrap"
        and isinstance(func.value, ast.Name)
    ):
        return func.value.id
    return None


def _module_constants(trees: dict[Path, ast.Module]) -> dict[str, set[str]]:
    """Every module-level ``NAME = "literal"`` across the package, by name."""
    constants: dict[str, set[str]] = {}
    for tree in trees.values():
        for node in tree.body:
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                for target in targets:
                    if isinstance(target, ast.Name):
                        constants.setdefault(target.id, set()).add(value.value)
    return constants


def _resolve(value: ast.expr, constants: dict[str, set[str]], where: str) -> str | None:
    """A literal or a constant's value; ``None`` for anything else (a variable)."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.Name) and value.id in constants:
        values = constants[value.id]
        assert len(values) == 1, f"{where}: {value.id} names several constants"
        return next(iter(values))
    return None


def scanned_codes() -> set[str]:
    """The codes rules (a)-(d) find in ``src/whygraph/``."""
    trees = {path: ast.parse(path.read_text()) for path in sorted(SRC.rglob("*.py"))}
    constants = _module_constants(trees)
    codes: set[str] = set()
    for path, tree in trees.items():
        rel = path.relative_to(ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node.func) in ERROR_CALLS:
                for keyword in node.keywords:
                    if keyword.arg == "code":
                        found = _resolve(
                            keyword.value, constants, f"{rel}:{node.lineno}"
                        )
                        if found is not None:
                            codes.add(found)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "code":
                        found = _resolve(value, constants, f"{rel}:{node.lineno}")
                        if found is not None:
                            codes.add(found)
            elif (
                isinstance(node, ast.FunctionDef)
                and node.name == "_sse_error_code"
                and path == SRC / "serve" / "chat.py"
            ):
                for inner in ast.walk(node):
                    if (
                        isinstance(inner, ast.Return)
                        and isinstance(inner.value, ast.Constant)
                        and isinstance(inner.value.value, str)
                    ):
                        codes.add(inner.value.value)
    return codes


def test_the_scan_finds_each_rule() -> None:
    """Each rule contributes: a literal, a dict body, a constant, an SSE frame."""
    codes = scanned_codes()
    assert "slug_taken" in codes  # (a) ApiError(..., code="slug_taken")
    assert "github_app_not_configured" in codes  # (b) webhook.py's hand-built body
    assert "budget_exceeded" in codes  # (c) code=BUDGET_EXCEEDED
    assert "llm_unavailable" in codes  # (d) _sse_error_code
    assert "symbol_not_found" in codes  # (a) ServeError / WhyGraphError
    assert "blame_failed" in codes  # (a) WhyGraphError.wrap(..., code=...)


def test_the_json_lists_every_backend_code() -> None:
    """``api-error-codes.json`` equals the scan plus :data:`VARIABLE_CODES`."""
    listed = json.loads(CODES_JSON.read_text())
    assert listed == sorted(set(listed)), "keep the JSON sorted and without duplicates"
    expected = scanned_codes() | VARIABLE_CODES
    missing = sorted(expected - set(listed))
    stale = sorted(set(listed) - expected)
    assert not missing, f"add to api-error-codes.json and lib/apiErrors.ts: {missing}"
    assert not stale, f"no backend code emits these any more: {stale}"


# ---------------------------------------------------------------------------
# The Explorer / Chat error body (serve/errors.py)
# ---------------------------------------------------------------------------


def _render(exc) -> tuple[int, dict]:
    from whygraph.serve.errors import whygraph_error_handler

    response = whygraph_error_handler(None, exc)  # type: ignore[arg-type]
    return response.status_code, json.loads(response.body)


def test_a_serve_error_renders_its_status_and_a_top_level_code() -> None:
    from whygraph.serve.errors import ServeError

    status, body = _render(ServeError(503, "no index", code="not_indexed"))
    assert (status, body) == (503, {"error": "no index", "code": "not_indexed"})


def test_a_coded_whygraph_error_keeps_the_status_rule() -> None:
    from whygraph.mcp.errors import WhyGraphError

    assert _render(
        WhyGraphError("'x' not found in CodeGraph", code="symbol_not_found")
    ) == (
        404,
        {"error": "'x' not found in CodeGraph", "code": "symbol_not_found"},
    )
    assert _render(
        WhyGraphError.wrap("git blame failed", OSError("x"), code="blame_failed")
    ) == (
        400,
        {"error": "git blame failed: x", "code": "blame_failed"},
    )
    # A codeless one keeps its old body.
    assert _render(WhyGraphError("limit must be >= 1")) == (
        400,
        {"error": "limit must be >= 1"},
    )


def test_a_viewer_refusal_is_coded_and_a_budget_refusal_unchanged() -> None:
    from whygraph.mcp.rationale import GenerationNotPermitted

    status, body = _render(GenerationNotPermitted())
    assert (status, body["code"]) == (400, "generation_not_permitted")
    status, body = _render(
        GenerationNotPermitted(reason="budget_exceeded", scope="org")
    )
    assert (status, body["code"], body["scope"]) == (403, "budget_exceeded", "org")
