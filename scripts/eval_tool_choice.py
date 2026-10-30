"""Measure which tool the chat assistant reaches for first, per question class.

Acceptance criterion 14 of ``plans/chat-evidence-tools-plan.md``. The plan's
whole thesis is that the specialized tools now *can* answer the three jobs the
assistant has — planning, debugging, statistics — so the model will stop falling
back to ``read_file`` / ``list_dir``. No unit test can check that: tool
selection is the model's judgement, given the descriptions.

So this drives live providers through the real HTTP surface, records the ordered
``tool_call`` names **and arguments** from the SSE stream, and prints a matrix.

Two stats tools over two databases moved the baseline, and added a failure mode
no single-tool eval could have: the model reaching the *wrong database*. So there
are now two kinds of check here, reported separately because they need different
fixes:

* **Selection** — which tool it opened with, and whether a structural question
  leaked into the history DB or vice versa.
* **Obedience** — whether the tool *description* was followed, read off the
  emitted arguments. Did the identity query resolve through the ``author`` table
  rather than grouping a raw git identity (and without ``json_each``, which the
  authorizer denies, or a ``LIKE`` against the emails array, which silently
  misattributes commits)? Did ``render_chart`` follow its producer and name
  columns rather than retyping values? Did a breakdown become one stacked chart
  rather than two? A miss here means the wording needs work, not that the
  feature is broken.

**Tool choice is per-model behaviour**, so a result from one model is evidence
about that model and nothing else. That is why targets are plural: run every
model you can reach, and a divergence between them shows up as a column that
disagrees rather than as a silent assumption.

Deliberately **not** pytest: it calls a paid provider and is non-deterministic,
so it must never gate CI. It is committed anyway because it is the only
regression guard on tool *selection*, and it will be wanted again the next time
the tool surface grows.

Usage
-----
Start a dev portal in one shell (data dir outside the checkout; the portal
keeps its data in Postgres, e.g. the ``make dev-db`` one), then add,
initialize and scan this repository from its Projects page::

    WHYGRAPH_SHARED_FOLDERS=$(cd .. && pwd) \\
    WHYGRAPH_DATABASE_URL=postgresql+psycopg://whygraph:whygraph-dev@127.0.0.1:55432/whygraph \\
      uv run whygraph portal --data "${XDG_STATE_HOME:-$HOME/.local/state}/whygraph-dev" --port 8321

Then, from the repository root (``--slug`` is the project's slug in the
portal; it defaults to this checkout's directory name)::

    # every provider the server reports as configured
    SSL_CERT_FILE=/etc/ssl/cert.pem \\
      uv run python scripts/eval_tool_choice.py --all-configured

    # or specific targets, `provider` or `provider:model`
    uv run python scripts/eval_tool_choice.py \\
      --target deepseek:deepseek-v4-flash --target deepseek:deepseek-v4-pro

Project notes (plan 40)::

    # a suite with a `setup.notes` block creates and confirms its `[eval] `
    # notes through /api/projects/<slug>/notes first (idempotent by body)
    uv run python scripts/eval_tool_choice.py --suite tests/fixtures/chat_eval/engineer.json \\
      --slug whygraph --mode engineer --target anthropic:claude-sonnet-5-5

    # retire every `[eval] ` note of the slug afterwards (one run per slug)
    uv run python scripts/eval_tool_choice.py --cleanup-notes --slug whygraph

Notes
-----
* A target whose turns all fail is reported **SKIPPED** with the provider's
  error, not scored as ten misses — an unusable credential is a gap in
  coverage, and dressing it up as a failing model would be worse than useless.
* ``[chat].provider`` defaults to ``anthropic``. "Configured" only means a key
  is *present*: a present-but-revoked key still 401s, which is exactly the case
  ``--all-configured`` is built to surface rather than hide.
* On a corporate network the LLM SDKs need ``SSL_CERT_FILE=/etc/ssl/cert.pem``
  or every turn fails with a bare "Connection error".
* **Record the real matrix, including misses.** A failed criterion is a finding
  to act on, not a number to massage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from whygraph.chat.grounding import _NUMBER_TOKEN, _SHA_TOKEN, _is_sha
from whygraph.core.config import LlmConfig
from whygraph.portal.prices import NO_OVERRIDES, price_for
from whygraph.services.llm import LlmClientFactory
from whygraph.services.llm.types import CompletionRequest

# Each case is (class, question, tools that count as the RIGHT first reach).
# "Right" is about the *first* tool: a good investigation interleaves several,
# but which one it starts from is what the descriptions actually control.
CASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "structure",
        "What's in the chat harness package?",
        # `search_symbols` is accepted here, and that is a CORRECTION made after
        # both DeepSeek models opened with it. `get_area_outline` needs a
        # repo-relative path, and "the chat harness package" is not one — so
        # expecting the outline call *first* demanded the model guess a path,
        # which is the one thing the tool descriptions tell it never to do.
        # Resolving the path, then outlining it, is the correct chain.
        ("get_area_outline", "search_symbols"),
    ),
    (
        "structure",
        "Who calls run_turn?",
        ("search_symbols", "get_symbol"),
    ),
    (
        "structure",
        "Give me an overview of the serve subsystem's modules.",
        ("get_area_outline",),
    ),
    (
        "debugging",
        "Chat replies vanish after the turn finishes — what changed recently?",
        ("find_changes", "list_recent_activity"),
    ),
    (
        "debugging",
        "Something broke in the serve chat router. What has been touched there?",
        ("find_changes", "get_area_history"),
    ),
    (
        "debugging",
        "The provider dropdown resets itself. Which commits were about that?",
        ("find_changes",),
    ),
    (
        "planning",
        "How would I add a new chat provider?",
        ("get_area_outline", "get_rationale", "search_symbols"),
    ),
    (
        "statistics",
        "How has commit volume changed month over month?",
        ("run_project_stats",),
    ),
    (
        "statistics",
        "Which files change most often in this repo?",
        ("run_project_stats",),
    ),
    (
        "statistics",
        "What's the average time from PR open to merge?",
        ("run_project_stats",),
    ),
    # The two-database boundary — the failure mode a second stats tool creates.
    # A structural question must not reach the history DB and vice versa, and no
    # amount of SQL cleverness in either can cover for the other.
    (
        "graph-stats",
        "Which modules have the most functions?",
        ("run_graph_stats",),
    ),
    (
        "graph-stats",
        "What is the distribution of symbol kinds in this codebase?",
        ("run_graph_stats",),
    ),
    # Identity: the one case that checks a _SCHEMA_DOC rule was OBEYED rather
    # than merely present. Possible only because rule 5 is mechanical.
    (
        "identity",
        "Which developer has been working the most lately?",
        ("run_project_stats",),
    ),
    (
        "identity",
        "Who are the top contributors?",
        ("run_project_stats",),
    ),
    # Charting: two rounds, in order, within the round limit.
    (
        "chart",
        "How has commit volume changed month over month? Show me a chart.",
        ("run_project_stats",),
    ),
    (
        "chart",
        "Break file changes down by change type per month, as a chart.",
        ("run_project_stats",),
    ),
)

_STATS_TOOL = "run_project_stats"
_GRAPH_STATS_TOOL = "run_graph_stats"
_CHART_TOOL = "render_chart"
_CODEGRAPH_TOOLS = ("get_area_outline", "search_symbols", "get_symbol")
_FILE_TOOLS = ("read_file", "list_dir")

# Every portal /api request must carry the custom header (the CSRF guard).
_HEADERS = {"Content-Type": "application/json", "X-WhyGraph-Client": "1"}

# Classes whose questions are about history, so `run_graph_stats` reaching them
# is the two-database confusion rather than a harmless extra call.
_HISTORY_CLASSES = frozenset({"statistics", "identity", "chart", "debugging"})


def _first_index(names: list[str], wanted: tuple[str, ...]) -> float:
    """Position of the first name in ``wanted``, or infinity if absent.

    Infinity is the point: "never reached" must compare as *worse* than any
    position, so a class that skipped the right tool entirely cannot pass by
    also skipping the wrong one.
    """
    for index, name in enumerate(names):
        if name in wanted:
            return index
    return float("inf")


def _post(url: str, payload: dict | None) -> object:
    """POST JSON and decode the JSON response."""
    body = json.dumps(payload).encode() if payload is not None else b"{}"
    request = urllib.request.Request(url, data=body, headers=_HEADERS)
    with urllib.request.urlopen(request) as response:  # noqa: S310 -- localhost only
        return json.loads(response.read())


def _stream_turn(base: str, session_id: int, question: str) -> dict:
    """Send one turn and return everything the SSE stream said about it.

    The **arguments** matter as much as the names now: a chart case has to check
    that `render_chart` named columns rather than retyping values, and the
    identity case has to check the emitted SQL resolved identity through the
    `author` table. Neither is visible in a list of tool names.

    Returns
    -------
    dict
        ``names``, ``calls`` (name + arguments), ``error``, the final ``answer``
        text, ``rounds`` (rounds that called tools), ``tool_errors``,
        the ``done`` frame's token counts and the turn's ``latency`` in seconds.
    """
    body = json.dumps({"content": question}).encode()
    request = urllib.request.Request(
        f"{base}/chat/sessions/{session_id}/messages",
        data=body,
        headers=_HEADERS,
    )
    turn: dict = {
        "names": [],
        "calls": [],
        "error": "",
        "answer": "",
        "rounds": 0,
        "tool_errors": 0,
        "misuse": [],
        "note_limit": False,
        "input_tokens": None,
        "output_tokens": None,
    }
    in_results = True  # a tool_call right after a tool_result opens a new round
    started = time.monotonic()
    with urllib.request.urlopen(request) as response:  # noqa: S310 -- localhost only
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            frame = json.loads(line[5:].strip())
            kind = frame.get("type")
            if kind == "tool_call":
                if in_results:
                    turn["rounds"] += 1
                    in_results = False
                turn["names"].append(frame["name"])
                turn["calls"].append(
                    {"name": frame["name"], "arguments": frame.get("arguments") or {}}
                )
            elif kind == "tool_result":
                in_results = True
                if _result_is_error(frame.get("result", "")):
                    turn["tool_errors"] += 1
                misuse = _misuse_kind(frame.get("result", ""))
                if misuse:
                    turn["misuse"].append(misuse)
                if NOTE_LIMIT_RE.search(frame.get("result", "")):
                    turn["note_limit"] = True
            elif kind == "text_delta":
                turn["answer"] += frame.get("text", "")
            elif kind == "error":
                turn["error"] = frame.get("message", "unknown error")
            elif kind == "done":
                turn["input_tokens"] = frame.get("input_tokens")
                turn["output_tokens"] = frame.get("output_tokens")
    turn["latency"] = time.monotonic() - started
    return turn


# The exact ``json.dumps`` output of ``ToolRegistry.dispatch`` for a call to a
# tool that does not exist and for wrong argument names (``chat/tools.py``
# ``dispatch`` and ``_encode``). Matched as a prefix of the frame's ``result``:
# a substring test would fire on repository text that quotes the phrase.
UNKNOWN_TOOL_PREFIX = '{"error": "unknown tool '
INVALID_ARGS_PREFIX = '{"error": "invalid arguments for '


NOTE_LIMIT_RE = re.compile(
    r'^\{"error": "[^"]*agent proposals (are waiting for review|per (member|project) per day)'
)
"""A ``propose_note`` result refused by a ``note_limit`` cap.

The chat tool body returns ``{"error": <message>}`` without the code, so the
caps' messages (``mcp/notes.py`` ``_check_caps``) are matched as a prefix.
"""


def _misuse_kind(result: str) -> str:
    """``unknown_tool`` / ``invalid_arguments`` for a misused call, else ``""``."""
    if result.startswith(UNKNOWN_TOOL_PREFIX):
        return "unknown_tool"
    if result.startswith(INVALID_ARGS_PREFIX):
        return "invalid_arguments"
    return ""


def _result_is_error(result: str) -> bool:
    """Whether a ``tool_result`` frame carries the harness's ``{"error": ...}``."""
    try:
        value = json.loads(result)
    except ValueError:
        return False
    return isinstance(value, dict) and "error" in value


def _stream_tool_calls(
    base: str, session_id: int, question: str
) -> tuple[list, list, str]:
    """Send one turn and return its tool names, the full calls, and any error."""
    turn = _stream_turn(base, session_id, question)
    return turn["names"], turn["calls"], turn["error"]


def _obedience_notes(klass: str, calls: list[dict]) -> list[str]:
    """Per-class checks that a *name* cannot express.

    These are the ones that measure whether a tool *description* was obeyed, not
    just which tool was picked. A miss here means the wording needs work — it does
    not mean the feature is broken, which is why they are reported separately from
    the first-tool verdict.
    """
    notes: list[str] = []
    sql = " ".join(
        str(call["arguments"].get("sql", ""))
        for call in calls
        if call["name"] in (_STATS_TOOL, _GRAPH_STATS_TOOL)
    ).lower()
    charts = [call for call in calls if call["name"] == _CHART_TOOL]

    if klass == "identity" and sql:
        # Rule 5: identity comes from the `author` table, never from a raw git
        # identity — one human routinely has several.
        if "author " not in sql and "author\n" not in sql and " author" not in sql:
            notes.append("SQL never mentions the author table")
        if "group by" in sql and (
            "group by c.author_name" in sql
            or "group by author_name" in sql
            or "group by c.author_email" in sql
            or "group by author_email" in sql
        ):
            notes.append("grouped by a RAW git identity (rule 5 disobeyed)")
        # The two forms the rule explicitly forbids: one is denied by the
        # authorizer, the other silently misattributes commits.
        if "json_each" in sql:
            notes.append("used json_each (denied by the authorizer)")
        if "like" in sql and "a.emails" in sql:
            notes.append("LIKE against author.emails (false-merges on '_')")

    if klass == "chart":
        if not charts:
            notes.append("no render_chart call")
        else:
            spec = charts[0]["arguments"]
            # The whole contract: columns, never values.
            for key in ("x", "y"):
                if not isinstance(spec.get(key), str):
                    notes.append(f"{key} is not a column name")
            if isinstance(spec.get("y"), list):
                notes.append("y is a list (two measures, not a breakdown)")
            # `render_chart` must follow its producer, not precede it.
            order = [call["name"] for call in calls]
            if order.index(_CHART_TOOL) == 0:
                notes.append("render_chart called before any stats query")
        # The stacked case: one chart with a `series`, not two charts.
        if (
            charts
            and "change type"
            in " ".join(str(v) for v in charts[0]["arguments"].values()).lower()
        ):
            spec = charts[0]["arguments"]
            if spec.get("kind") not in ("bar_stacked", "bar_h_stacked"):
                notes.append("breakdown asked for, but kind is not stacked")
            elif not spec.get("series"):
                notes.append("stacked kind without a series column")

    return notes


def _run_target(base: str, provider: str | None, model: str | None) -> list[tuple]:
    """Run every case against one provider/model and return the raw rows."""
    session_body: dict = {}
    if provider:
        session_body["provider"] = provider
    if model:
        session_body["model"] = model

    rows: list[tuple] = []
    for klass, question, expected in CASES:
        session = _post(f"{base}/chat/sessions", session_body or None)
        names, calls, error = _stream_tool_calls(base, session["id"], question)
        first = names[0] if names else "(none)"
        # A general SQL tool cannibalising the specialized ones is the original
        # risk; a *structural* SQL tool answering a temporal question is the new
        # one. Both are "the wrong database reached", so both count as a leak.
        leaked = (
            klass not in _HISTORY_CLASSES | {"graph-stats"} and _STATS_TOOL in names
        ) or (klass in _HISTORY_CLASSES and _GRAPH_STATS_TOOL in names)
        notes = _obedience_notes(klass, calls)
        rows.append(
            (klass, question, first, names, first in expected, leaked, error, notes)
        )
    return rows


def _clauses(rows: list[tuple]) -> list[tuple[str, bool]]:
    """Criterion 14's three clauses, scored per class.

    Separate from the first-tool metric on purpose: that one is stricter than
    the plan asks, and "wrong opening move" needs a different fix from "never
    got there at all". Conflating them hides which happened.
    """
    return [
        (
            "structure: a CodeGraph tool before read_file/list_dir",
            all(
                _first_index(names, _CODEGRAPH_TOOLS) < _first_index(names, _FILE_TOOLS)
                for klass, _, _, names, *_ in rows
                if klass == "structure"
            ),
        ),
        (
            "debugging: find_changes or get_evidence appears",
            all(
                bool({"find_changes", "get_evidence"} & set(names))
                for klass, _, _, names, *_ in rows
                if klass == "debugging"
            ),
        ),
        (
            f"statistics: {_STATS_TOOL} appears there, and the wrong DB nowhere",
            all(not row[5] for row in rows)
            and all(
                _STATS_TOOL in names
                for klass, _, _, names, *_ in rows
                if klass == "statistics"
            ),
        ),
        (
            f"structure: {_GRAPH_STATS_TOOL} answers the code-shape questions",
            all(
                _GRAPH_STATS_TOOL in names
                for klass, _, _, names, *_ in rows
                if klass == "graph-stats"
            ),
        ),
        (
            f"charts: {_CHART_TOOL} follows a stats call, naming columns",
            all(not row[7] for row in rows if row[0] == "chart"),
        ),
        (
            "identity: resolved through the author table, not a raw git identity",
            all(not row[7] for row in rows if row[0] == "identity"),
        ),
        (
            "round limit: no charted question hit it",
            all(len(row[3]) <= 6 for row in rows if row[0] == "chart"),
        ),
    ]


def _report(label: str, rows: list[tuple]) -> tuple[int, int]:
    """Print one target's matrix. Returns ``(passes, leaks)``."""
    width = max(len(question) for _, question, *_ in rows)
    print(f"\n=== {label} ===")
    print(f"{'class':11} {'question':{width}} {'first tool':22} verdict")
    print("-" * (11 + width + 34))
    for klass, question, first, names, passed, leaked, error, notes in rows:
        verdict = "PASS" if passed else "MISS"
        if leaked:
            verdict += " +WRONG-DB"
        if error:
            verdict += f" [error: {error[:40]}]"
        print(f"{klass:11} {question:{width}} {first:22} {verdict}")
        if len(names) > 1:
            print(f"{'':11} {'':{width}} \u21b3 then: {', '.join(names[1:])}")
        for note in notes:
            print(f"{'':11} {'':{width}} \u2718 {note}")

    passes = sum(1 for row in rows if row[4])
    leaks = sum(1 for row in rows if row[5])
    print(f"\nfirst-tool correct: {passes}/{len(rows)}")
    print(f"a stats tool reached the WRONG database: {leaks}")
    disobeyed = sum(1 for row in rows if row[7])
    print(f"cases where a tool description was not obeyed: {disobeyed}")
    for clause, ok in _clauses(rows):
        print(f"  [{'PASS' if ok else 'FAIL'}] {clause}")
    return passes, leaks


def _all_failed(rows: list[tuple]) -> str:
    """The shared error when every single turn failed, else an empty string.

    A revoked key produces ten identical error frames and zero tool calls.
    Scoring that as a model with terrible tool selection would be actively
    misleading, so the caller reports it as SKIPPED instead.
    """
    errors = {row[6] for row in rows}
    if len(errors) == 1 and next(iter(errors)) and not any(row[3] for row in rows):
        return next(iter(errors))
    return ""


def _parse_targets(args) -> list[tuple[str | None, str | None]]:
    """Resolve CLI options into ``(provider, model)`` pairs."""
    if args.all_configured:
        providers = _post_get(f"{_project_api(args)}/chat/providers")
        configured = [p["provider"] for p in providers if p.get("configured")]
        if not configured:
            print("! no provider reports a key", file=sys.stderr)
        return [(name, None) for name in configured]
    if args.target:
        pairs = []
        for raw in args.target:
            provider, _, model = raw.partition(":")
            pairs.append((provider, model or None))
        return pairs
    # No target given: whatever [chat] defaults to.
    return [(args.provider, args.model)]


def _post_get(url: str) -> list:
    """GET JSON (the providers listing)."""
    request = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(request) as response:  # noqa: S310 -- localhost only
        return json.loads(response.read())


def _project_api(args) -> str:
    """The portal's scoped API prefix for the project under test."""
    return f"{args.base.rstrip('/')}/api/projects/{args.slug}"


# ---------------------------------------------------------------------------
# Suites: JSON cases scored over k trials (plan 38 section 5.12)
# ---------------------------------------------------------------------------


TURN_COST_MARGIN = 0.30
"""Worst-case cost of one turn, USD, for the ``--stop-above`` check."""


def _load_suite(path: str) -> dict:
    """Read a suite file: ``{suite, cases: [{id, question, expect}]}``."""
    suite = json.loads(Path(path).read_text())
    if not suite.get("cases"):
        raise SystemExit(f"! {path}: no cases")
    return suite


def _misuse_failures(turn: dict) -> list[str]:
    """``tool_misuse:<kind>`` per unknown-tool or bad-arguments result of a turn."""
    return [f"tool_misuse:{kind}" for kind in turn.get("misuse") or []]


def _check_case(expect: dict, turn: dict) -> list[str]:
    """The failed expectations of one trial; empty means the trial passed.

    ``tools_any``: at least one of these was called. ``tools_never``: none was.
    ``args``: ``{tool: {key: regex}}``, every named tool call must have the key
    matching (``re.search``) in at least one of its calls. ``cites``: regexes the
    final answer must match. ``forbid``: regexes the final answer must not
    match (the never-rank cases). ``max_rounds``: tool rounds at most this many.
    A turn that got an ``unknown tool`` or ``invalid arguments`` tool result
    always fails (``tool_misuse``).
    """
    failures: list[str] = []
    if turn["error"]:
        failures.append(f"turn error: {turn['error'][:60]}")
    failures += _misuse_failures(turn)
    names = set(turn["names"])
    wanted = expect.get("tools_any") or []
    if wanted and not names & set(wanted):
        failures.append(f"none of {', '.join(wanted)} called")
    for tool in expect.get("tools_never") or []:
        if tool in names:
            failures.append(f"{tool} called")
    for tool, keys in (expect.get("args") or {}).items():
        calls = [c["arguments"] for c in turn["calls"] if c["name"] == tool]
        for key, pattern in keys.items():
            if not any(re.search(pattern, str(c.get(key, ""))) for c in calls):
                failures.append(f"{tool}.{key} !~ {pattern}")
    for pattern in expect.get("cites") or []:
        if not re.search(pattern, turn["answer"]):
            failures.append(f"answer does not cite {pattern}")
    for pattern in expect.get("forbid") or []:
        if re.search(pattern, turn["answer"]):
            failures.append(f"answer matches forbidden {pattern[:30]}")
    limit = expect.get("max_rounds")
    if limit is not None and turn["rounds"] > limit:
        failures.append(f"{turn['rounds']} rounds > {limit}")
    return failures


def _usage_items(args, session_id: int) -> list[dict]:
    """Every ``/api/usage/calls`` item of one chat session (all pages)."""
    url = (
        f"{args.base.rstrip('/')}/api/usage/calls"
        f"?project={args.slug}&chat_session={session_id}"
    )
    items: list[dict] = []
    page_url = url
    for _ in range(200):
        page = _get_json(page_url)
        items += page.get("items", [])
        following = page.get("next")
        if not following:
            break
        page_url = f"{url}&before={urllib.parse.quote(following)}"
    return items


def _sum_usage(items: list[dict]) -> dict | None:
    """Split a session's usage items into chat spend and side spend.

    The token columns and ``cost`` sum only ``task == "chat"`` items. Nested
    rationale generation and description backfill rows carry the same chat
    session id; they are reported as ``side_spend`` and never enter a keep
    rule. ``cost_usd`` arrives as a decimal string. ``None`` without items.
    """
    if not items:
        return None
    chat = [i for i in items if i.get("task") == "chat"]

    def total(rows: list[dict], key: str) -> int:
        return sum(int(i.get(key) or 0) for i in rows)

    def money(rows: list[dict]) -> float:
        return sum(float(i.get("cost_usd") or 0.0) for i in rows)

    read = total(chat, "cache_read_tokens")
    write = total(chat, "cache_write_tokens")
    return {
        "cost": money(chat),
        "input_tokens": total(chat, "input_tokens"),
        "output_tokens": total(chat, "output_tokens"),
        "cache_read_tokens": read,
        "cache_write_tokens": write,
        "uncached_in": total(chat, "input_tokens") - read - write,
        "side_spend": money([i for i in items if i.get("task") != "chat"]),
    }


def _turn_cost(args, session_id: int) -> dict | None:
    """The session's spend from ``GET /api/usage/calls``, or None if unavailable.

    Usage rows are written by a background thread, so poll until the sums are
    stable across two reads. Returns the :func:`_sum_usage` dict.
    """
    total: dict | None = None
    for _ in range(12):
        time.sleep(0.5)
        try:
            seen = _sum_usage(_usage_items(args, session_id))
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if seen is not None and seen == total:
            return seen
        total = seen
    return total


def _get_json(url: str) -> dict:
    """GET a JSON object."""
    request = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(request) as response:  # noqa: S310 -- localhost only
        return json.loads(response.read())


def _spent(args) -> float:
    """The org's spend this month, from ``GET /api/budgets``."""
    return _get_json(f"{args.base.rstrip('/')}/api/budgets")["org"]["spent_usd"]


# ---------------------------------------------------------------------------
# Project notes: suite setup, per-case cleanup, --cleanup-notes (plan 40)
# ---------------------------------------------------------------------------

EVAL_PREFIX = "[eval] "
"""Every ``setup.notes`` body starts with this; ``--cleanup-notes`` finds them by it."""


def _notes_api(args) -> str:
    """The project's notes routes."""
    return f"{_project_api(args)}/notes"


def _list_notes(args, **params) -> list[dict]:
    """Every note matching ``params`` (the list route's filters), newest first."""
    found: list[dict] = []
    before: int | None = None
    while True:
        query = {"limit": 200, **params}
        if before is not None:
            query["before_id"] = before
        page = _post_get(f"{_notes_api(args)}?{urllib.parse.urlencode(query)}")
        found += page
        if len(page) < 200:
            return found
        before = min(note["id"] for note in page)


def _setup_notes(args, suite: dict) -> dict[str, int]:
    """Create and confirm a suite's ``setup.notes``; return ``{name: note id}``.

    Each entry is ``{"body", "kind", "anchor", "as"?, "confirm"?}``. Notes are
    matched by body among the non-retired ones, so a rerun reuses them: a
    proposal is confirmed unless ``"confirm": false``. A note that exists in the
    wrong state (confirmed where a proposal was wanted) stops the run, because
    the case would not test what it says. Only ``as``-named notes are returned.
    """
    entries = (suite.get("setup") or {}).get("notes") or []
    ids: dict[str, int] = {}
    if not entries:
        return ids
    existing = [n for n in _list_notes(args) if n.get("status") != "retired"]
    for entry in entries:
        body = entry["body"]
        if not body.startswith(EVAL_PREFIX):
            raise SystemExit(f"! setup note body must start with {EVAL_PREFIX!r}")
        wanted = "confirmed" if entry.get("confirm", True) else "proposed"
        note = next((n for n in existing if n["body"] == body), None)
        if note is None:
            payload = {k: entry[k] for k in ("body", "kind", "anchor") if k in entry}
            try:
                note = _post(_notes_api(args), payload)
            except urllib.error.HTTPError as exc:
                raise SystemExit(
                    f"! setup note refused: {exc.code} {exc.read()[:200]!r}"
                )
            existing.append(note)
        if note["status"] == "proposed" and wanted == "confirmed":
            note = _post(f"{_notes_api(args)}/{note['id']}/confirm", None)
        if note["status"] != wanted:
            raise SystemExit(
                f"! setup note #{note['id']} is {note['status']}, wanted {wanted}: "
                "run --cleanup-notes and retry"
            )
        if entry.get("as"):
            ids[entry["as"]] = note["id"]
    return ids


def _substitute(expect: dict, ids: dict[str, int]) -> dict:
    """``expect`` with every ``{<name>}`` of ``cites`` / ``forbid`` replaced by a note id.

    A literal ``str.replace`` per name, never ``str.format``: the patterns are
    regexes, and ``\\d{1,3}`` must survive.
    """
    if not ids:
        return expect
    out = dict(expect)
    for key in ("cites", "forbid"):
        patterns = []
        for pattern in expect.get(key) or []:
            for name, note_id in ids.items():
                pattern = pattern.replace("{" + name + "}", str(note_id))
            patterns.append(pattern)
        if key in expect:
            out[key] = patterns
    return out


def _expects_proposal(case: dict) -> bool:
    """Whether a case is a "remember that" case (it expects ``propose_note``)."""
    return "propose_note" in ((case.get("expect") or {}).get("tools_any") or [])


def _reject_session_proposals(args, session_id: int) -> int:
    """Retire the pending proposals a chat session made; return how many.

    One ``POST /notes/reject-agent-proposals`` per proposer found among the
    session's proposals (``origin`` ``chat:<session id>``).
    """
    origin = f"chat:{session_id}"
    proposers = {
        n["proposed_by"]
        for n in _list_notes(args, status="proposed")
        if n.get("origin") == origin
    }
    return sum(
        _post(
            f"{_notes_api(args)}/reject-agent-proposals",
            {"proposed_by": uid, "origin": origin},
        )["rejected"]
        for uid in sorted(proposers)
    )


def _cleanup_notes(args) -> int:
    """``--cleanup-notes``: retire every non-retired ``[eval] `` note of ``--slug``.

    A proposal is retired as ``rejected``, a confirmed note as ``outdated`` (the
    only reasons each state accepts). One run per slug; returns 0.
    """
    count = 0
    for status, reason in (("proposed", "rejected"), ("confirmed", "outdated")):
        for note in _list_notes(args, status=status):
            if note["body"].startswith(EVAL_PREFIX):
                _post(f"{_notes_api(args)}/{note['id']}/retire", {"reason": reason})
                count += 1
    print(f"retired {count} [eval] note(s) in {args.slug}")
    return 0


# ---------------------------------------------------------------------------
# Deterministic checks: cites_exist (every cited SHA and #N is real)
# ---------------------------------------------------------------------------

# Grounding's SHA rule (`_SHA_TOKEN` + `_is_sha`), bounded by non-alphanumerics
# so a backticked or bare 7-40 char hex run inside a longer word is not a SHA.
_SHA_BOUNDED = re.compile(rf"(?<![0-9A-Za-z]){_SHA_TOKEN.pattern}(?![0-9A-Za-z])")


_NOTE_LIST = re.compile(
    r"\bnotes\s+(#\d{1,9}(?:(?:\s*,\s*and\s+|\s*,\s*|\s+and\s+|\s+or\s+|\s*&\s*)#\d{1,9})*)"
    r"|\bnote\s+(#\d{1,9})",
    re.IGNORECASE,
)
"""``note #12`` (one id) and ``notes #3, #4 and #5`` (a list): the ids are notes."""


def _extract_refs(answer: str) -> tuple[list[str], list[int], list[int]]:
    """The commit SHAs (lowercased), ``#N`` numbers and note ids an answer cites.

    ``deadbeef`` (no digit), ``added`` (too short) and ``20261029`` (no
    letter) are not SHAs. A ``#N`` that follows ``note `` is a note id (and so
    is every id of a ``notes #3, #4 and #5`` list); it is returned as the
    third result and never as a PR or issue number. An HTML entity
    (``&#39;``) is not a reference.
    """
    shas: list[str] = []
    for match in _SHA_BOUNDED.finditer(answer):
        token = match.group(0)
        if _is_sha(token) and token.lower() not in shas:
            shas.append(token.lower())
    note_ids: list[int] = []
    note_starts: set[int] = set()
    for listed in _NOTE_LIST.finditer(answer):
        group = 1 if listed.group(1) else 2
        for item in _NUMBER_TOKEN.finditer(listed.group(group)):
            note_starts.add(listed.start(group) + item.start())
            number = int(item.group(1))
            if number not in note_ids:
                note_ids.append(number)
    numbers: list[int] = []
    for match in _NUMBER_TOKEN.finditer(answer):
        before = answer[max(0, match.start() - 1) : match.start()]
        if match.start() in note_starts or before == "&":
            continue  # a note reference, or an HTML entity such as &#39;
        number = int(match.group(1))
        if number not in numbers:
            numbers.append(number)
    return shas, numbers, note_ids


def _ref_failures(
    answer: str,
    resolve_sha: Callable[[str], str],
    resolve_number: Callable[[int], bool],
    pr_sha: Callable[[str, list[int]], bool] | None = None,
    resolve_note: Callable[[int], str] | None = None,
) -> tuple[list[str], list[str]]:
    """``(failures, notes)`` of one answer's cited refs.

    ``resolve_sha`` returns ``ok``, ``missing``, ``ambiguous``,
    ``unverified`` or ``identifier``; ``resolve_number`` whether ``#N`` is a
    PR or an issue. A miss is ``invented_ref:<ref>``; an ambiguous prefix, an
    unverifiable one (no checkout to ask git) and a hex token that is not a
    commit but appears in the repository's files (an Alembic revision id, a
    hash constant) are notes, not failures. So is a SHA git cannot resolve
    that is the head or merge commit of a ``#N`` the same answer cites
    (``pr_sha``): a squash-merged PR's branch commit is never in the clone.

    ``resolve_note`` returns ``confirmed``, ``unconfirmed`` or ``missing`` for
    a cited ``note #N``: ``missing`` fails ``invented_ref:note#N`` and
    ``unconfirmed`` (proposed or retired) fails ``unconfirmed_ref:note#N``, so a
    cited proposal never counts as real. Without it note ids are not checked.
    """
    shas, numbers, note_ids = _extract_refs(answer)
    failures: list[str] = []
    notes: list[str] = []
    for sha in shas:
        verdict = resolve_sha(sha)
        if verdict == "missing" and pr_sha is not None and pr_sha(sha, numbers):
            notes.append(f"pr_sha_ref:{sha}")
        elif verdict == "missing":
            failures.append(f"invented_ref:{sha}")
        elif verdict in ("ambiguous", "unverified", "identifier"):
            notes.append(f"{verdict}_ref:{sha}")
    for number in numbers:
        if not resolve_number(number):
            failures.append(f"invented_ref:#{number}")
    if resolve_note is not None:
        for note_id in note_ids:
            verdict = resolve_note(note_id)
            if verdict == "missing":
                failures.append(f"invented_ref:note#{note_id}")
            elif verdict != "confirmed":
                failures.append(f"unconfirmed_ref:note#{note_id}")
    return failures, notes


def _body_resolves(url: str) -> bool:
    """Whether a lookup route answered a JSON body without an ``error`` key.

    Never judged by HTTP status: these routes answer a miss with 200 and
    ``{"error": "not_found"}``.
    """
    try:
        body = _get_json(url)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except ValueError:
            return False
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return isinstance(body, dict) and "error" not in body


_ROOTS: dict[str, str | None] = {}


def _project_root(args) -> str | None:
    """The project's checkout, the ``root`` of ``GET /api/projects/{slug}``."""
    if args.slug not in _ROOTS:
        try:
            _ROOTS[args.slug] = _get_json(_project_api(args)).get("root")
        except (urllib.error.URLError, OSError, ValueError):
            _ROOTS[args.slug] = None
    return _ROOTS[args.slug]


def _git(root: str, *argv: str) -> subprocess.CompletedProcess:
    """Run git in ``root`` ignoring the global config."""
    return subprocess.run(
        ["git", "-C", root, *argv],
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull},
    )


def _live_resolvers(
    args,
) -> tuple[
    Callable[[str], str],
    Callable[[int], bool],
    Callable[[str, list[int]], bool],
    Callable[[int], str],
]:
    """The portal-backed resolvers :func:`_ref_failures` takes."""
    api = _project_api(args)

    def resolve_sha(sha: str) -> str:
        root = _project_root(args)
        if not root or not Path(root).is_dir():
            return "unverified"
        found = _git(root, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
        if found.returncode != 0:
            candidates = _git(root, "rev-parse", f"--disambiguate={sha}")
            if len(candidates.stdout.split()) > 1:
                return "ambiguous"
            # Not a commit, but a real identifier the repository contains
            # (an Alembic revision id such as `6f1b3d8a2c94`): not invented.
            if _git(root, "grep", "-q", "-I", "-i", "-F", sha).returncode == 0:
                return "identifier"
            return "missing"
        return (
            "ok"
            if _body_resolves(f"{api}/commit/{found.stdout.strip()}")
            else "missing"
        )

    def resolve_number(number: int) -> bool:
        return _body_resolves(f"{api}/pr/{number}") or _body_resolves(
            f"{api}/issue/{number}"
        )

    def pr_sha(sha: str, numbers: list[int]) -> bool:
        for number in numbers:
            try:
                pr = _get_json(f"{api}/pr/{number}").get("pull_request") or {}
            except (urllib.error.URLError, OSError, ValueError):
                continue
            if any(
                str(pr.get(key) or "").startswith(sha)
                for key in ("head_sha", "merge_commit_sha")
            ):
                return True
        return False

    def resolve_note(note_id: int) -> str:
        """``confirmed``, ``unconfirmed`` or ``missing`` (a 404 body has ``error``)."""
        try:
            body = _get_json(f"{api}/notes/{note_id}")
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
            except ValueError:
                return "missing"
        except (urllib.error.URLError, OSError, ValueError):
            return "missing"
        if not isinstance(body, dict) or "error" in body:
            return "missing"
        return "confirmed" if body.get("status") == "confirmed" else "unconfirmed"

    return resolve_sha, resolve_number, pr_sha, resolve_note


# ---------------------------------------------------------------------------
# Multi-turn cases
# ---------------------------------------------------------------------------


def _case_turns(case: dict) -> list[str]:
    """The questions of a case: its ``turns``, else its one ``question``."""
    return list(case["turns"]) if case.get("turns") else [case["question"]]


def _merge_turns(turns: list[dict]) -> dict:
    """One trial dict from a multi-turn case's per-turn dicts.

    Tokens and latency are summed over the turns; ``rounds`` and ``answer``
    are the last turn's; tool names, calls and misuse cover every turn.
    ``expect`` is checked against the last turn only (the caller does that
    before merging); ``turn_answers`` keeps every answer for the ref check.
    """
    last = turns[-1]
    merged = dict(last)
    merged["names"] = [n for t in turns for n in t["names"]]
    merged["calls"] = [c for t in turns for c in t["calls"]]
    merged["misuse"] = [m for t in turns for m in t["misuse"]]
    merged["note_limit"] = any(t.get("note_limit") for t in turns)
    merged["tool_errors"] = sum(t["tool_errors"] for t in turns)
    merged["error"] = next((t["error"] for t in turns if t["error"]), "")
    merged["latency"] = sum(t["latency"] for t in turns)
    for key in ("input_tokens", "output_tokens"):
        values = [t[key] for t in turns if t[key] is not None]
        merged[key] = sum(values) if values else None
    merged["turn_answers"] = [t["answer"] for t in turns]
    return merged


def _run_case_turns(
    args,
    session_id: int,
    case: dict,
    resolvers: tuple[Callable, ...] | None = None,
) -> tuple[dict, list[str], list[str]]:
    """Send a case's turns in one session; return ``(merged, failures, notes)``.

    ``expect`` applies to the last turn only. ``tool_misuse`` and
    ``cites_exist`` run on every turn.
    """
    resolvers = resolvers or _live_resolvers(args)
    per_turn: list[dict] = []
    for question in _case_turns(case):
        per_turn.append(_stream_turn(_project_api(args), session_id, question))
        if per_turn[-1]["error"]:
            break
    failures = _check_case(case.get("expect") or {}, per_turn[-1])
    notes: list[str] = []
    for index, turn in enumerate(per_turn):
        if index < len(per_turn) - 1:
            failures += _misuse_failures(turn)
        if case.get("cites_exist", True):
            more, found = _ref_failures(turn["answer"], *resolvers)
            failures += more
            notes += found
    return _merge_turns(per_turn), list(dict.fromkeys(failures)), notes


def _run_suite_pass(
    args, provider: str | None, model: str | None, suite: dict, k: int
) -> list[dict]:
    """Run every case ``k`` times, a fresh session per trial; return trial dicts."""
    session_body: dict = {}
    if provider:
        session_body["provider"] = provider
    if model:
        session_body["model"] = model
    if args.mode:
        session_body["mode"] = args.mode
    trials: list[dict] = []
    note_ids = suite.get("_note_ids") or {}
    for case in suite["cases"]:
        turns = _case_turns(case)
        if note_ids:
            case = {**case, "expect": _substitute(case.get("expect") or {}, note_ids)}
        for trial in range(1, k + 1):
            if args.stop_above is not None and (
                _spent(args) + TURN_COST_MARGIN * len(turns) > args.stop_above
            ):
                print(
                    f"! org spend would cross {args.stop_above}: stopping before "
                    f"{case['id']} t{trial}",
                    file=sys.stderr,
                )
                return trials
            session = _post(f"{_project_api(args)}/chat/sessions", session_body or None)
            turn, failures, notes = _run_case_turns(args, session["id"], case)
            if _expects_proposal(case):
                _reject_session_proposals(args, session["id"])
            if turn.get("note_limit"):
                print(
                    f"! ABORTED at {case['id']} t{trial}: propose_note was refused "
                    "by a note_limit cap (not scored as a pass or a fail); at most "
                    "four full runs per UTC day, or --cleanup-notes and wait",
                    file=sys.stderr,
                )
                return trials
            usage = _turn_cost(args, session["id"])
            turn.update(
                case=case["id"],
                question=turns[-1],
                turns=turns,
                trial=trial,
                session_id=session["id"],
                failures=failures,
                notes=notes,
                passed=not failures,
                usage=usage,
                cost=usage["cost"] if usage else None,
                tokens_total=(
                    usage["input_tokens"] + usage["output_tokens"] if usage else None
                ),
            )
            trials.append(turn)
            cost = f"${turn['cost']:.3f}" if turn["cost"] is not None else "$?"
            print(
                f"  {case['id']:4} t{trial} {'PASS' if turn['passed'] else 'FAIL'} "
                f"{turn['rounds']}r {turn['latency']:.0f}s {cost} "
                f"{'; '.join(failures)}",
                flush=True,
            )
    return trials


def _mean(values: list) -> float:
    """Mean of the non-None values, 0.0 when there are none."""
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else 0.0


def _usage_field(trial: dict, key: str):
    """One field of a trial's usage sums, or None without usage rows."""
    usage = trial.get("usage")
    return usage[key] if usage else None


def _trial_tokens(trial: dict) -> int:
    """Whole-turn tokens (in+out): the usage sum, else the ``done`` frames' total."""
    if trial.get("tokens_total") is not None:
        return trial["tokens_total"]
    return (trial["input_tokens"] or 0) + (trial["output_tokens"] or 0)


def _cache_fraction(trial: dict) -> float | None:
    """Cache-read share of the input tokens, or None without usage."""
    usage = trial.get("usage")
    if not usage or not usage["input_tokens"]:
        return None
    return usage["cache_read_tokens"] / usage["input_tokens"]


def _summarize(trials: list[dict]) -> dict:
    """Aggregate trials: pass rate, pass^k, rounds, tokens, latency, errors."""
    by_case: dict[str, list[dict]] = {}
    for t in trials:
        by_case.setdefault(t["case"], []).append(t)
    calls = sum(len(t["names"]) for t in trials)
    return {
        "trials": len(trials),
        "pass_rate": _mean([1.0 if t["passed"] else 0.0 for t in trials]),
        "pass_k": _mean(
            [1.0 if all(t["passed"] for t in ts) else 0.0 for ts in by_case.values()]
        ),
        "rounds": _mean([t["rounds"] for t in trials]),
        "tokens": _mean([_trial_tokens(t) for t in trials]),
        "cache_pct": _mean([_cache_fraction(t) for t in trials]),
        "uncached_in": _mean([_usage_field(t, "uncached_in") for t in trials]),
        "side_spend": _mean([_usage_field(t, "side_spend") for t in trials]),
        "side_spend_total": sum(_usage_field(t, "side_spend") or 0.0 for t in trials),
        "latency": _mean([t["latency"] for t in trials]),
        "tool_error_rate": (sum(t["tool_errors"] for t in trials) / calls)
        if calls
        else 0.0,
        "cost": _mean([t["cost"] for t in trials]),
        "cost_total": sum(t["cost"] or 0.0 for t in trials),
        "tools": Counter(n for t in trials for n in t["names"]),
        "by_case": by_case,
    }


def _suite_markdown(
    label: str, args, runs: list[dict], spent: tuple[float, float]
) -> str:
    """The Markdown report for the suites run against one target."""
    per_suite = ", ".join(r["suite"] + "=" + str(r["k"]) for r in runs)
    out = [
        f"# Chat eval report - {label}",
        "",
        f"- Date: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- Portal: {args.base}, project `{args.slug}`, mode: {args.mode or '(portal default, none sent)'}",
        f"- k per suite: {per_suite}, warm-up: {'yes (discarded)' if args.warmup else 'no'}",
        f"- Org spend: {spent[0]:.4f} -> {spent[1]:.4f} USD (delta {spent[1] - spent[0]:.4f})",
        "",
    ]
    for run in runs:
        summ = _summarize(run["trials"])
        out += [
            f"## Suite `{run['suite']}` (k={run['k']})",
            "",
            f"- Pass rate: {summ['pass_rate']:.0%} of {summ['trials']} trials",
            f"- pass^{run['k']}: {summ['pass_k']:.0%} of cases",
            f"- Mean tool rounds: {summ['rounds']:.2f}",
            f"- Mean tokens (in+out, whole turn): {summ['tokens']:.0f}",
            f"- Mean cache %: {summ['cache_pct']:.0%} (cache read / input)",
            f"- Mean uncached in: {summ['uncached_in']:.0f}",
            f"- Side spend (non-chat usage rows of the sessions): "
            f"${summ['side_spend']:.4f} per turn (total ${summ['side_spend_total']:.2f})",
            f"- Mean latency: {summ['latency']:.1f}s",
            f"- Tool-error rate: {summ['tool_error_rate']:.1%} of tool calls",
            f"- Cost per turn: ${summ['cost']:.4f} (total ${summ['cost_total']:.2f})",
            "- Tool counts: "
            + ", ".join(f"{n} {c}" for n, c in summ["tools"].most_common()),
            "",
            "| case | passed | pass^k | rounds | tokens (whole turn) | cache % | uncached in | latency | cost | side spend | failures |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for case_id, ts in summ["by_case"].items():
            one = _summarize(ts)
            fails = "; ".join(sorted({f for t in ts for f in t["failures"]}))
            out.append(
                f"| {case_id} | {sum(t['passed'] for t in ts)}/{len(ts)} "
                f"| {'yes' if one['pass_k'] else 'no'} | {one['rounds']:.1f} "
                f"| {one['tokens']:.0f} | {one['cache_pct']:.0%} "
                f"| {one['uncached_in']:.0f} | {one['latency']:.0f}s "
                f"| ${one['cost']:.3f} | ${one['side_spend']:.3f} | {fails or '-'} |"
            )
        out.append("")
    return "\n".join(out)


def _suite_main(args, targets: list[tuple[str | None, str | None]]) -> int:
    """Run ``--suite`` files over each target; write the report and sidecar."""
    suites = [_load_suite(path) for path in args.suite]
    suite_k = {name: int(n) for name, _, n in (a.partition("=") for a in args.suite_k)}
    for suite in suites:
        suite["_note_ids"] = _setup_notes(args, suite)
    for provider, model in targets:
        label = f"{provider or '(config default)'}{':' + model if model else ''}"
        before = _spent(args)
        if args.warmup:
            print(f"warm-up (k=1, discarded) on {label}")
            for suite in suites:
                _run_suite_pass(args, provider, model, suite, 1)
        runs = []
        for suite in suites:
            k = suite_k.get(suite["suite"], args.k)
            print(f"suite {suite['suite']} k={k} on {label}")
            trials = _run_suite_pass(args, provider, model, suite, k)
            runs.append(
                {
                    "suite": suite["suite"],
                    "k": k,
                    "mode": args.mode,
                    "slug": args.slug,
                    "root": _project_root(args),
                    "trials": trials,
                }
            )
        report = _suite_markdown(label, args, runs, (before, _spent(args)))
        out = Path(args.report)
        out.write_text(report)
        sidecar = out.with_suffix(".json")
        sidecar.write_text(
            json.dumps(
                {
                    "target": label,
                    "mode": args.mode,
                    "k": args.k,
                    "runs": [
                        {
                            **run,
                            "trials": [
                                {k: v for k, v in t.items() if k != "calls"}
                                | {"tool_calls": t["calls"]}
                                for t in run["trials"]
                            ],
                        }
                        for run in runs
                    ],
                },
                indent=2,
            )
        )
        print(f"wrote {out} and {sidecar}")
    return 0


# ---------------------------------------------------------------------------
# Offline replay: rebuild every request of a report's sessions, no model calls
# ---------------------------------------------------------------------------

DEFAULT_PRICE_TARGET = ("anthropic", "claude-sonnet-5-5")
CACHE_LOOKBACK_BLOCKS = 20
"""Anthropic's cache lookback: a hit is found within this many blocks."""


def _blocks(message) -> int:
    """Content blocks a message adds to the cache lookback count."""
    return 1 + len(message.tool_calls)


def _shared_prefix(previous: list, current: list) -> int:
    """How many leading messages two requests share."""
    count = 0
    for old, new in zip(previous, current):
        if old != new:
            break
        count += 1
    return count


def _modelled_cost(
    previous: list | None,
    current: list,
    *,
    fixed_tokens: int,
    price,
) -> tuple[int, float]:
    """``(cached_tokens, usd)`` of one request, an estimate and nothing more.

    The part shared with the session's previous request is priced at
    ``cache_read`` when its first changed block lies within
    :data:`CACHE_LOOKBACK_BLOCKS` of the previous request's last block;
    otherwise only the fixed prefix (system and tools) is read. Everything
    else is priced at ``cache_write``. Output tokens are excluded.
    ``fixed_tokens`` is the system and tool-spec estimate.
    """
    from whygraph.chat.harness import _estimate_tokens

    total = sum(_estimate_tokens(m) for m in current) + fixed_tokens
    cached = 0
    if previous is not None:
        shared = _shared_prefix(previous, current)
        shared_blocks = sum(_blocks(m) for m in previous[:shared])
        previous_blocks = sum(_blocks(m) for m in previous)
        if previous_blocks - shared_blocks <= CACHE_LOOKBACK_BLOCKS:
            cached = fixed_tokens + sum(_estimate_tokens(m) for m in current[:shared])
        else:
            system = min(shared, _system_count(current))
            cached = fixed_tokens + sum(_estimate_tokens(m) for m in current[:system])
    cached = min(cached, total)
    read = float(price.cache_read if price.cache_read is not None else price.input)
    write = float(price.cache_write if price.cache_write is not None else price.input)
    usd = (cached * read + (total - cached) * write) / 1_000_000
    return cached, usd


def _system_count(messages: list) -> int:
    """How many leading messages are system messages."""
    count = 0
    for message in messages:
        if message.role != "system":
            break
        count += 1
    return count


def _replay_session(
    session_id: int,
    mode: str | None,
    *,
    budget: int,
    price,
    system: tuple,
    specs_tokens: int,
) -> list[dict]:
    """Rebuild every request of one stored session; return a record per turn.

    A turn's history is ``serve.chat._load_history`` up to that turn's user
    row (it merges continuations, sanitizes and re-appends grounding, as the
    live turn did); its rounds are the rows after the user row. Each request
    is :func:`~whygraph.chat.harness.begin_turn_messages` plus
    :func:`~whygraph.chat.harness._trim_turn` behind the system messages, so
    a change to either reaches the replay. Must run under ``use_project``.
    """
    from whygraph.chat.harness import (
        _estimate_tokens,
        _trim_turn,
        begin_turn_messages,
    )
    from whygraph.db import get_session
    from whygraph.serve.chat import _load_history, _path_ids, _session_rows

    with get_session() as db:
        rows = _session_rows(db, session_id)
        path = _path_ids(rows, max(rows) if rows else None)
        roles = {row_id: rows[row_id].role for row_id in path}
    user_ids = [i for i in path if roles[i] == "user"]
    fixed = sum(_estimate_tokens(m) for m in system) + specs_tokens
    previous: list | None = None
    turns: list[dict] = []
    for position, user_id in enumerate(user_ids):
        end = (
            path[path.index(user_ids[position + 1]) - 1]
            if position + 1 < len(user_ids)
            else path[-1]
        )
        history = _load_history(session_id, user_id)
        rounds = _load_history(session_id, end)[len(history) :]
        messages, turn_start = begin_turn_messages(history, token_budget=budget)
        requests: list[dict] = []
        index = tool_rounds = 0
        latest_round = turn_start
        while True:
            current = [*system, *messages]
            cached, usd = _modelled_cost(
                previous, current, fixed_tokens=specs_tokens, price=price
            )
            requests.append(
                {
                    "est_tokens": sum(_estimate_tokens(m) for m in current)
                    + specs_tokens,
                    "fixed_tokens": fixed,
                    "cached_tokens": cached,
                    "modelled_usd": round(usd, 6),
                }
            )
            previous = current
            if index >= len(rounds) or rounds[index].role != "assistant":
                break
            reply = rounds[index]
            index += 1
            if not reply.tool_calls:
                break
            tool_rounds += 1
            latest_round = len(messages)
            messages.append(reply)
            while index < len(rounds) and rounds[index].role == "tool":
                messages.append(rounds[index])
                index += 1
            _trim_turn(messages, turn_start, latest_round, tool_rounds, budget)
        turns.append({"user_row_id": user_id, "requests": requests})
    return turns


def _price_target(label: str):
    """``(provider, model, Price, version)`` for a report's target label."""
    from whygraph.portal.prices import NO_OVERRIDES, price_for

    provider, _, model = label.partition(":")
    if not provider or provider.startswith("("):
        provider, model = DEFAULT_PRICE_TARGET
    model = model or DEFAULT_PRICE_TARGET[1]
    found = price_for(provider, model, NO_OVERRIDES, custom_endpoint=False)
    if found is None:
        raise SystemExit(f"! no bundled price for {provider}:{model}")
    return provider, model, found[0], found[1]


def _replay_main(args) -> int:
    """``--replay``: rebuild a report's requests in-process; write the JSON."""
    from dataclasses import replace

    from whygraph.chat.harness import _specs_tokens, build_system_prompt
    from whygraph.chat.modes import get_mode
    from whygraph.chat.tools import ToolRegistry
    from whygraph.core import Config
    from whygraph.core.config import ChatConfig
    from whygraph.core.context import ProjectContext, use_project
    from whygraph.portal.paths import project_db_path

    report = json.loads(Path(args.replay).read_text())
    data_dir = Path(args.data_dir).resolve()
    budget = args.context_budget or ChatConfig().context_token_budget
    provider, model, price, version = _price_target(report.get("target", ""))
    saved = (
        json.loads(Path(args.overview_from).read_text()).get("overviews", {})
        if args.overview_from
        else {}
    )
    overviews: dict[str, str] = {}
    runs_out: list[dict] = []
    for run in report["runs"]:
        slug = run["slug"]
        root = run.get("root")
        if not root:
            root = _get_json(f"{args.base.rstrip('/')}/api/projects/{slug}")["root"]
        config = replace(
            Config.defaults(),
            whygraph_db=project_db_path(data_dir, "local", slug),
            codegraph_db=Path(root) / ".codegraph" / "codegraph.db",
            chat=ChatConfig(context_token_budget=budget),
        )
        with use_project(ProjectContext(slug=slug, root=Path(root), config=config)):
            registry = ToolRegistry(max_rationale_generations=0)
            stable, overview = build_system_prompt(registry, get_mode(run.get("mode")))
            if slug in saved:
                overview = replace(overview, content=saved[slug])
            overviews[slug] = overview.content
            specs_tokens = _specs_tokens(registry.specs)
            trials = []
            for trial in run["trials"]:
                if not trial.get("session_id"):
                    continue
                turns = _replay_session(
                    trial["session_id"],
                    run.get("mode"),
                    budget=budget,
                    price=price,
                    system=(stable, overview),
                    specs_tokens=specs_tokens,
                )
                trials.append(
                    {
                        "case": trial["case"],
                        "trial": trial["trial"],
                        "session_id": trial["session_id"],
                        "turns": turns,
                        "est_tokens": sum(
                            r["est_tokens"] for t in turns for r in t["requests"]
                        ),
                        "modelled_usd": round(
                            sum(
                                r["modelled_usd"] for t in turns for r in t["requests"]
                            ),
                            6,
                        ),
                    }
                )
        runs_out.append(
            {
                "suite": run["suite"],
                "slug": slug,
                "mode": run.get("mode"),
                "trials": trials,
            }
        )
    out = {
        "source_report": str(args.replay),
        "context_token_budget": budget,
        "tool_count": len(registry.specs),
        "price": {
            "provider": provider,
            "model": model,
            "version": version,
            "cache_read": float(price.cache_read or price.input),
            "cache_write": float(price.cache_write or price.input),
        },
        "cost_note": (
            "modelled_usd is an ESTIMATE: tokens are len//4, a request is priced "
            "at cache_read for the part shared with the session's previous "
            f"request when the first changed block is within {CACHE_LOOKBACK_BLOCKS} "
            "blocks of its last block (else only system and tools are read), "
            "the rest at cache_write; output excluded."
        ),
        "overviews": overviews,
        "runs": runs_out,
    }
    Path(args.out).write_text(json.dumps(out, indent=2))
    total = sum(t["modelled_usd"] for r in runs_out for t in r["trials"])
    print(f"wrote {args.out}: modelled input cost ${total:.4f} (estimate)")
    return 0


# ---------------------------------------------------------------------------
# Blind judge: packets and tally (plan 38 section 12 Q8)
# ---------------------------------------------------------------------------

RUBRIC_PATH = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "chat_eval"
    / "judge_rubric.md"
)


def _answers_by_mode(report: dict) -> dict[tuple[str, str], dict[str, dict]]:
    """``{(suite, case): {mode: {question, answer, tool_calls}}}`` from a report.

    A run's mode is its own ``mode`` (falling back to the report's). The first
    trial without an error and with a non-empty answer is the one judged.
    """
    found: dict[tuple[str, str], dict[str, dict]] = {}
    for run in report["runs"]:
        mode = run.get("mode") or report.get("mode")
        for trial in run["trials"]:
            if trial.get("error") or not (trial.get("answer") or "").strip():
                continue
            slot = found.setdefault((run["suite"], trial["case"]), {})
            slot.setdefault(
                mode,
                {
                    "question": trial["question"],
                    "answer": trial["answer"],
                    "tool_calls": [
                        {"name": c["name"], "arguments": c.get("arguments", {})}
                        for c in trial.get("tool_calls", [])
                    ],
                },
            )
    return found


def _write_packets(report_path: str, out_dir: str) -> int:
    """Write one packet per (question, own-mode vs other mode, order) and a key.

    A question's own mode is its suite's name. Packet ids are neutral and
    shuffled with a fixed seed; ``key.json`` is the only file that names modes.
    """
    import random

    report = json.loads(Path(report_path).read_text())
    rubric = RUBRIC_PATH.read_text()
    work: list[tuple[str, str, str, str, str]] = []
    for (suite, case), by_mode in sorted(_answers_by_mode(report).items()):
        if suite not in by_mode:
            print(
                f"! {case}: no usable answer in its own mode, skipped", file=sys.stderr
            )
            continue
        for other in sorted(m for m in by_mode if m != suite):
            work.append((case, suite, suite, other, "own-first"))
            work.append((case, suite, other, suite, "own-second"))
    random.Random(38).shuffle(work)
    out = Path(out_dir)
    (out / "votes").mkdir(parents=True, exist_ok=True)
    answers = _answers_by_mode(report)
    key: dict[str, dict] = {}
    for number, (case, suite, mode_a, mode_b, _) in enumerate(work, 1):
        packet_id = f"p{number:03d}"
        side = answers[(suite, case)]
        packet = {
            "packet": packet_id,
            "question": side[suite]["question"],
            "A": {
                "answer": side[mode_a]["answer"],
                "tool_calls": side[mode_a]["tool_calls"],
            },
            "B": {
                "answer": side[mode_b]["answer"],
                "tool_calls": side[mode_b]["tool_calls"],
            },
            "rubric": rubric,
        }
        (out / f"{packet_id}.json").write_text(json.dumps(packet, indent=2))
        key[packet_id] = {"A": mode_a, "B": mode_b, "question_id": case, "suite": suite}
    (out / "key.json").write_text(json.dumps(key, indent=2))
    print(f"wrote {len(key)} packets to {out} (key.json kept beside them)")
    return len(key)


def _tally_votes(out_dir: str) -> str:
    """Count ``votes/*.json`` against ``key.json``; return the Markdown table.

    Per (question, other mode) the votes of both orders are pooled and the
    majority decides (a split is a tie). A mode wins a question when it beats or
    ties both other modes and beats at least one.
    """
    out = Path(out_dir)
    key = json.loads((out / "key.json").read_text())
    if any(info.get("kind") == "compare" for info in key.values()):
        return _tally_compare(out, key)
    pooled: dict[tuple[str, str, str], Counter] = {}
    for path in sorted((out / "votes").glob("*.json")):
        data = json.loads(path.read_text())
        for vote in data if isinstance(data, list) else [data]:
            info = key.get(vote.get("packet"))
            if info is None or vote.get("winner") not in ("A", "B", "tie"):
                continue
            own, other = info["suite"], None
            other = info["B"] if info["A"] == own else info["A"]
            tally = pooled.setdefault((info["question_id"], own, other), Counter())
            if vote["winner"] == "tie":
                tally["tie"] += 1
            else:
                tally[info[vote["winner"]]] += 1
    verdicts: dict[tuple[str, str], dict[str, str]] = {}
    for (case, own, other), tally in pooled.items():
        a, b = tally[own], tally[other]
        verdicts.setdefault((own, case), {})[other] = (
            "win" if a > b else "loss" if b > a else "tie"
        )
    modes = sorted({v["suite"] for v in key.values()})
    lines = [
        "| mode | questions won | questions | votes (own / other / tie per pair) |",
        "|---|---|---|---|",
    ]
    for mode in modes:
        cases = sorted(c for (m, c) in verdicts if m == mode)
        won = sum(
            1
            for c in cases
            if all(r in ("win", "tie") for r in verdicts[(mode, c)].values())
            and any(r == "win" for r in verdicts[(mode, c)].values())
        )
        pairs = "; ".join(
            f"{c}/{o}: {pooled[(c, mode, o)][mode]}/{pooled[(c, mode, o)][o]}"
            f"/{pooled[(c, mode, o)]['tie']}"
            for c in cases
            for o in sorted(verdicts[(mode, c)])
        )
        lines.append(f"| {mode} | {won} | {len(cases)} | {pairs} |")
    return "\n".join(lines)


def _flat_answers(report: dict) -> dict[tuple[str, str], dict]:
    """``{(suite, case): {question, answer, tool_calls}}``, one answer each.

    A run with several modes yields the answer of the mode named like the
    suite, else the first mode.
    """
    flat: dict[tuple[str, str], dict] = {}
    for (suite, case), by_mode in _answers_by_mode(report).items():
        flat[(suite, case)] = by_mode.get(suite) or next(iter(by_mode.values()))
    return flat


def _write_compare_packets(
    baseline_path: str, candidate_path: str, out_dir: str
) -> int:
    """Write baseline-versus-candidate packets and ``key.json``.

    One packet per (case, order) for every (suite, case) with a usable answer
    in both reports. ``key.json`` maps ``A`` / ``B`` to ``baseline`` /
    ``candidate`` and is the only file that names them.
    """
    import random

    base = _flat_answers(json.loads(Path(baseline_path).read_text()))
    cand = _flat_answers(json.loads(Path(candidate_path).read_text()))
    rubric = RUBRIC_PATH.read_text()
    work: list[tuple[str, str, str, str]] = []
    for suite, case in sorted(set(base) & set(cand)):
        work.append((suite, case, "baseline", "candidate"))
        work.append((suite, case, "candidate", "baseline"))
    for suite, case in sorted(set(base) ^ set(cand)):
        print(f"! {case}: answer in only one report, skipped", file=sys.stderr)
    random.Random(39).shuffle(work)
    out = Path(out_dir)
    (out / "votes").mkdir(parents=True, exist_ok=True)
    sides = {"baseline": base, "candidate": cand}
    key: dict[str, dict] = {}
    for number, (suite, case, side_a, side_b) in enumerate(work, 1):
        packet_id = f"p{number:03d}"
        a, b = sides[side_a][(suite, case)], sides[side_b][(suite, case)]
        packet = {
            "packet": packet_id,
            "question": a["question"],
            "A": {"answer": a["answer"], "tool_calls": a["tool_calls"]},
            "B": {"answer": b["answer"], "tool_calls": b["tool_calls"]},
            "rubric": rubric,
        }
        (out / f"{packet_id}.json").write_text(json.dumps(packet, indent=2))
        key[packet_id] = {
            "A": side_a,
            "B": side_b,
            "question_id": case,
            "suite": suite,
            "kind": "compare",
        }
    (out / "key.json").write_text(json.dumps(key, indent=2))
    print(f"wrote {len(key)} packets to {out} (key.json kept beside them)")
    return len(key)


def _majority(wins: Counter) -> str:
    """``candidate`` / ``baseline`` when one strictly leads, else ``tie``."""
    if wins["candidate"] > wins["baseline"]:
        return "candidate"
    if wins["baseline"] > wins["candidate"]:
        return "baseline"
    return "tie"


def _tally_compare(out: Path, key: dict) -> str:
    """Candidate wins / ties / losses per suite, per family and pooled.

    Within a family the votes on both orders of a case are pooled and the
    majority decides (a split is a tie). The agreement rate is the share of
    packets voted by two or more families where every family's majority
    winner is the same.
    """
    by_packet: dict[str, dict[str, Counter]] = {}
    for path in sorted((out / "votes").glob("*.json")):
        data = json.loads(path.read_text())
        for vote in data if isinstance(data, list) else [data]:
            info = key.get(vote.get("packet"))
            if info is None or vote.get("winner") not in ("A", "B", "tie"):
                continue
            family = vote.get("judge_family") or "unknown"
            who = "tie" if vote["winner"] == "tie" else info[vote["winner"]]
            by_packet.setdefault(vote["packet"], {}).setdefault(family, Counter())[
                who
            ] += 1
    # (suite, case) -> family -> Counter of baseline / candidate / tie votes
    cases: dict[tuple[str, str], dict[str, Counter]] = {}
    for packet_id, families in by_packet.items():
        info = key[packet_id]
        slot = cases.setdefault((info["suite"], info["question_id"]), {})
        for family, votes in families.items():
            slot.setdefault(family, Counter()).update(votes)
    rows: dict[tuple[str, str], Counter] = {}
    for (suite, _), families in cases.items():
        pooled = Counter()
        for family, votes in families.items():
            rows.setdefault((suite, family), Counter())[_majority(votes)] += 1
            pooled.update(votes)
        rows.setdefault((suite, "pooled"), Counter())[_majority(pooled)] += 1
    lines = [
        "| suite | family | candidate wins | ties | candidate losses |",
        "|---|---|---|---|---|",
    ]
    for (suite, family), tally in sorted(
        rows.items(), key=lambda kv: (kv[0][0], kv[0][1] == "pooled", kv[0][1])
    ):
        lines.append(
            f"| {suite} | {family} | {tally['candidate']} | {tally['tie']} "
            f"| {tally['baseline']} |"
        )
    shared = [f for f in by_packet.values() if len(f) >= 2]
    agree = sum(1 for f in shared if len({_majority(v) for v in f.values()}) == 1)
    rate = f"{agree}/{len(shared)} ({agree / len(shared):.0%})" if shared else "n/a"
    lines.append("")
    lines.append(f"agreement (packets judged by 2+ families): {rate}")
    return "\n".join(lines)


def _judge_family(provider: str, model: str) -> str:
    """The model's vendor: the ``vendor/`` prefix of a routed id, else the provider."""
    return model.split("/", 1)[0] if "/" in model else provider


def _parse_vote(text: str, packet_id: str, family: str) -> dict | None:
    """Parse a judge reply into a vote ``_tally_votes`` reads, or ``None``."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    winner = data.get("winner") if isinstance(data, dict) else None
    if winner not in ("A", "B", "tie"):
        return None
    return {
        "packet": packet_id,
        "winner": winner,
        "reason": str(data.get("reason", ""))[:500],
        "judge_family": family,
    }


def _judge_inputs(packet: dict) -> tuple[str, str]:
    """``(system, prompt)`` for one packet: the rubric, then the packet body."""
    rubric = packet.get("rubric") or RUBRIC_PATH.read_text()
    body = {k: v for k, v in packet.items() if k != "rubric"}
    return rubric, json.dumps(body, indent=2)


def _judge_run(
    spec: str,
    out_dir: str,
    max_usd: float,
    price_in: float | None = None,
    price_out: float | None = None,
    max_tokens: int = 1000,
) -> int:
    """Judge every packet in ``out_dir`` with ``PROVIDER:MODEL``; write votes.

    The model is priced, the whole run is estimated and checked against
    ``max_usd`` before any client exists; a refusal returns 2 with nothing
    sent. The client comes from environment keys only.
    """
    provider, _, model = spec.partition(":")
    if not provider or not model:
        print("! --judge-run needs PROVIDER:MODEL", file=sys.stderr)
        return 2
    priced = price_for(provider, model, NO_OVERRIDES, custom_endpoint=False)
    if priced is not None:
        rate_in, rate_out = float(priced[0].input), float(priced[0].output)
        source = priced[1]
    elif price_in is None or price_out is None:
        print(
            f"! {spec} is not in prices.json: pass --judge-price-in and "
            "--judge-price-out (USD per million tokens, from the provider's page)",
            file=sys.stderr,
        )
        return 2
    else:
        rate_in, rate_out, source = price_in, price_out, "operator"
    out = Path(out_dir)
    packets = [
        json.loads(p.read_text())
        for p in sorted(out.glob("p*.json"))
        if p.name != "key.json"
    ]
    inputs = [_judge_inputs(p) for p in packets]
    per_packet = [
        (len(system) + len(prompt)) / 4 * rate_in / 1e6 + max_tokens * rate_out / 1e6
        for system, prompt in inputs
    ]
    estimate = sum(per_packet)
    if estimate > max_usd:
        print(
            f"! estimated ${estimate:.4f} for {len(packets)} packets exceeds "
            f"--judge-max-usd ${max_usd:.4f}; nothing sent",
            file=sys.stderr,
        )
        return 2
    client = LlmClientFactory(LlmConfig()).make(provider, model=model)
    family = _judge_family(provider, model)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", spec)
    (out / "votes").mkdir(exist_ok=True)
    spent, written, unparsed = 0.0, 0, []
    for packet, (system, prompt), reserve in zip(packets, inputs, per_packet):
        if spent + reserve > max_usd:
            print(f"! cap reached after {written} votes", file=sys.stderr)
            break
        resp = client.complete(
            CompletionRequest.of(prompt, system=system, max_tokens=max_tokens)
        )
        spent += (
            float(resp.cost_usd)
            if resp.cost_usd is not None
            else (resp.input_tokens or 0) / 1e6 * rate_in
            + (resp.output_tokens or 0) / 1e6 * rate_out
        )
        vote = _parse_vote(resp.text, packet["packet"], family)
        if vote is None:
            unparsed.append(packet["packet"])
            continue
        (out / "votes" / f"{packet['packet']}-{safe}.json").write_text(
            json.dumps(vote, indent=2)
        )
        written += 1
    (out / f"judge-{safe}.json").write_text(
        json.dumps(
            {
                "model": spec,
                "judge_family": family,
                "price_source": source,
                "price_in": rate_in,
                "price_out": rate_out,
                "estimate_usd": round(estimate, 6),
                "spent_usd": round(spent, 6),
                "votes": written,
                "unparsed": unparsed,
            },
            indent=2,
        )
    )
    print(f"{written} votes, est ${estimate:.4f}, spent ${spent:.4f}")
    return 0


def _self_test_refs() -> bool:
    """``cites_exist`` extraction, the not-found body and the misuse prefixes."""
    answer = (
        "deadbeef and added and 20261029 and 1698765432 are not refs; "
        "a1b2c3d, `0123456789ab` and A1B2C3D are. note #12 is a note, "
        "see #7 and (#9). Entity &#39; is not a ref."
    )
    shas, numbers, note_ids = _extract_refs(answer)
    ok = shas == ["a1b2c3d", "0123456789ab"] and numbers == [7, 9]
    ok &= note_ids == [12]  # `note #12` is a note, never `#12`
    ok &= _extract_refs("sha deadbeefdeadbeef1 here") == (
        ["deadbeefdeadbeef1"],
        [],
        [],
    )
    failures, notes = _ref_failures(
        "fixed in a1b2c3d, abc1234 and ffff123, see #7 and #8",
        lambda sha: {"a1b2c3d": "ok", "abc1234": "ambiguous"}.get(sha, "missing"),
        lambda number: number == 7,
    )
    ok &= failures == ["invented_ref:ffff123", "invented_ref:#8"]
    ok &= notes == ["ambiguous_ref:abc1234"]
    failures, notes = _ref_failures(
        "revision 6f1b3d8a2c94", lambda sha: "identifier", bool
    )
    ok &= failures == [] and notes == ["identifier_ref:6f1b3d8a2c94"]
    failures, notes = _ref_failures(
        "#12597 has one commit, `0f2c5bd`; `abc1234` is made up",
        lambda sha: "missing",
        lambda number: True,
        lambda sha, numbers: sha == "0f2c5bd" and numbers == [12597],
    )
    ok &= failures == ["invented_ref:abc1234"] and notes == ["pr_sha_ref:0f2c5bd"]
    real = globals()["_get_json"]
    try:
        globals()["_get_json"] = lambda url: {"error": "not_found"}
        ok &= _body_resolves("x") is False
        globals()["_get_json"] = lambda url: {"sha": "a1b2c3d"}
        ok &= _body_resolves("x") is True
    finally:
        globals()["_get_json"] = real
    ok &= _misuse_kind('{"error": "unknown tool \'nope\'"}') == "unknown_tool"
    ok &= _misuse_kind('{"error": "invalid arguments for get_pr: x"}') == (
        "invalid_arguments"
    )
    ok &= _misuse_kind('{"commits": ["{\\"error\\": \\"unknown tool "]}') == ""
    ok &= _check_case({}, {**_fake_turn(), "misuse": ["unknown_tool"]}) == [
        "tool_misuse:unknown_tool"
    ]
    return bool(ok)


def _self_test_notes() -> bool:
    """Note references, their resolver, ``setup.notes`` and the substitution."""
    ok = _extract_refs("see note #12 and #7") == ([], [7], [12])
    ok &= _extract_refs("Notes #3 and #4 apply") == ([], [], [3, 4])
    ok &= _extract_refs("notes #3, #4, and #5; see #9") == ([], [9], [3, 4, 5])
    ok &= _extract_refs("notes #1 & #2 or #6") == ([], [], [1, 2, 6])
    ok &= _extract_refs("note #5, but PR #6") == ([], [6], [5])
    ok &= _extract_refs("&#39; is an entity, note: #8") == ([], [8], [])
    verdicts = {5: "confirmed", 6: "unconfirmed"}
    ok_sha, ok_pr = (lambda sha: "ok"), (lambda number: True)

    def resolve_note(note_id: int) -> str:
        return verdicts.get(note_id, "missing")

    failures, _ = _ref_failures("note #5", ok_sha, ok_pr, None, resolve_note)
    ok &= failures == []
    failures, _ = _ref_failures("note #99", ok_sha, ok_pr, None, resolve_note)
    ok &= failures == ["invented_ref:note#99"]  # missing
    failures, _ = _ref_failures("notes #5 and #6", ok_sha, ok_pr, None, resolve_note)
    ok &= failures == ["unconfirmed_ref:note#6"]  # a proposed note
    failures, _ = _ref_failures("note #99", ok_sha, ok_pr)
    ok &= failures == []  # no resolver: not checked
    # A note id is never looked up as a PR or an issue.
    looked_up: list[int] = []
    _ref_failures(
        "note #12", ok_sha, lambda n: looked_up.append(n) or True, None, resolve_note
    )
    ok &= looked_up == []
    # The live resolver: a 404 body (`error`) is missing, a status decides.
    args = argparse.Namespace(base="http://x", slug="s")
    real = globals()["_get_json"]
    try:
        bodies = {
            "1": {"id": 1, "status": "confirmed"},
            "2": {"id": 2, "status": "proposed"},
            "3": {"error": "not found", "code": "note_not_found"},
        }
        globals()["_get_json"] = lambda url: bodies[url.rsplit("/", 1)[1]]
        resolver = _live_resolvers(args)[3]
        ok &= [resolver(1), resolver(2), resolver(3)] == [
            "confirmed",
            "unconfirmed",
            "missing",
        ]
    finally:
        globals()["_get_json"] = real
    # {name} substitution is a literal replace: \d{1,3} survives.
    expect = {
        "cites": ["note #{confirmed}\\b", "\\d{1,3} days"],
        "forbid": ["note #{pending}\\b"],
        "tools_any": ["x"],
    }
    ids = {"confirmed": 14, "pending": 15}
    out = _substitute(expect, ids)
    ok &= out["cites"] == ["note #14\\b", "\\d{1,3} days"]
    ok &= out["forbid"] == ["note #15\\b"] and out["tools_any"] == ["x"]
    ok &= expect["cites"][0] == "note #{confirmed}\\b"  # the input is untouched
    ok &= _substitute(expect, {}) is expect
    # The substituted patterns decide a case: the proposal's id is forbidden.
    turn = _fake_turn(names=["x"], answer="Per note #14 ... also note #150")
    ok &= _check_case(out, turn) == ["answer does not cite \\d{1,3} days"]
    turn = _fake_turn(names=["x"], answer="note #14, 5 days; the draft note #15 says")
    ok &= _check_case(out, turn) == ["answer matches forbidden note #15\\b"]
    # setup.notes: matched by body (idempotent), confirmed unless confirm=false.
    sent: list[tuple[str, object]] = []
    store = [{"id": 20, "body": "[eval] have", "status": "proposed"}]

    def fake_post(url, payload):
        sent.append((url.rsplit("/notes", 1)[1], payload))
        if url.endswith("/confirm"):
            note = next(n for n in store if n["id"] == int(url.split("/")[-2]))
            note["status"] = "confirmed"
            return note
        note = {"id": 21 + len(store), "body": payload["body"], "status": "proposed"}
        store.append(note)
        return note

    suite = {
        "setup": {
            "notes": [
                {"body": "[eval] have", "kind": "decision", "anchor": {}, "as": "a"},
                {"body": "[eval] new", "kind": "gotcha", "anchor": {}, "as": "b"},
                {
                    "body": "[eval] draft",
                    "kind": "gotcha",
                    "anchor": {},
                    "as": "c",
                    "confirm": False,
                },
            ]
        }
    }
    patched = {
        "_post": fake_post,
        "_list_notes": lambda a, **kw: [dict(n) for n in store],
    }
    saved = {name: globals()[name] for name in patched}
    try:
        globals().update(patched)
        first = _setup_notes(args, suite)
        sent.clear()
        second = _setup_notes(args, suite)
    finally:
        globals().update(saved)
    ok &= first == {"a": 20, "b": 22, "c": 23} and second == first
    ok &= [n["status"] for n in store] == ["confirmed", "confirmed", "proposed"]
    ok &= not any(path == "" for path, _ in sent)  # the rerun created nothing
    try:
        globals().update(patched)
        _setup_notes(args, {"setup": {"notes": [{"body": "no prefix"}]}})
        ok = False
    except SystemExit:
        pass
    finally:
        globals().update(saved)
    # note_limit refusals are recognised in a propose_note result.
    ok &= bool(
        NOTE_LIMIT_RE.search(
            '{"error": "at most 20 agent proposals per member per day (UTC)"}'
        )
    )
    ok &= not NOTE_LIMIT_RE.search('{"status": "proposed", "note": "agent proposals"}')
    ok &= _expects_proposal({"expect": {"tools_any": ["propose_note"]}})
    ok &= not _expects_proposal({"expect": {"tools_any": ["get_pr"]}})
    return bool(ok)


def _fake_turn(**fields) -> dict:
    """A minimal per-turn dict as ``_stream_turn`` returns."""
    turn = {
        "names": [],
        "calls": [],
        "error": "",
        "answer": "",
        "rounds": 0,
        "tool_errors": 0,
        "misuse": [],
        "input_tokens": 10,
        "output_tokens": 5,
        "latency": 1.0,
    }
    return {**turn, **fields}


def _self_test_multi_turn() -> bool:
    """One session, one ``_turn_cost``, checks per turn and ``expect`` on the last."""
    import argparse

    args = argparse.Namespace(
        base="http://x", slug="s", mode=None, stop_above=None, warmup=False
    )
    scripted = iter(
        [
            _fake_turn(
                names=["search_symbols"],
                calls=[{"name": "search_symbols", "arguments": {}}],
                answer="first, see abc1234",
                rounds=1,
            ),
            _fake_turn(answer="second", rounds=0, misuse=["unknown_tool"]),
            _fake_turn(
                names=["get_commit"],
                calls=[{"name": "get_commit", "arguments": {}}],
                answer="third #3",
                rounds=2,
            ),
        ]
    )
    cost_calls: list[int] = []
    patched = {
        "_post": lambda url, payload: {"id": 41},
        "_stream_turn": lambda base, sid, question: next(scripted),
        "_turn_cost": lambda a, sid: (
            cost_calls.append(sid)
            or {
                "cost": 0.1,
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_tokens": 60,
                "cache_write_tokens": 10,
                "uncached_in": 30,
                "side_spend": 0.01,
            }
        ),
        "_spent": lambda a: 0.0,
        "_live_resolvers": lambda a: (
            lambda sha: "missing" if sha == "abc1234" else "ok",
            lambda number: True,
        ),
    }
    saved = {name: globals()[name] for name in patched}
    suite = {
        "suite": "t",
        "cases": [
            {
                "id": "M1",
                "turns": ["q1", "q2", "q3"],
                "expect": {"tools_any": ["get_commit"], "max_rounds": 2},
            }
        ],
    }
    try:
        globals().update(patched)
        trials = _run_suite_pass(args, None, None, suite, 1)
    finally:
        globals().update(saved)
    ok = len(trials) == 1 and cost_calls == [41]
    trial = trials[0]
    # expect (tools_any get_commit, rounds <= 2) holds on the LAST turn only;
    # the first turn's invented ref and the second's misuse fail the case.
    ok &= set(trial["failures"]) == {
        "invented_ref:abc1234",
        "tool_misuse:unknown_tool",
    }
    ok &= trial["rounds"] == 2 and trial["question"] == "q3"
    ok &= trial["input_tokens"] == 30 and trial["output_tokens"] == 15
    ok &= trial["latency"] == 3.0 and trial["tokens_total"] == 120
    ok &= trial["names"] == ["search_symbols", "get_commit"]
    summary = _summarize(trials)
    ok &= summary["tokens"] == 120 and round(summary["cache_pct"], 2) == 0.6
    ok &= summary["uncached_in"] == 30 and summary["side_spend"] == 0.01
    # --stop-above scales the margin by the number of turns.
    args.stop_above = 0.5
    globals()["_spent"] = lambda a: 0.0
    try:
        globals().update({k: v for k, v in patched.items() if k != "_spent"})
        scripted = iter([])  # a call to _stream_turn would raise StopIteration
        stopped = _run_suite_pass(args, None, None, suite, 1)
    finally:
        globals().update(saved)
    ok &= stopped == [] and TURN_COST_MARGIN * 3 > 0.5
    return bool(ok)


def _self_test_window() -> bool:
    """Which turns ``build_window`` elides for a 4-turn fixture."""
    from whygraph.chat.harness import ELIDED_MARKER, build_window
    from whygraph.services.llm.chat import ChatMessage, ToolCall

    history = []
    for n in range(4):
        call = ToolCall(id=f"t{n}", name="get_evidence", arguments={"q": n})
        history += [
            ChatMessage(role="user", content=f"question {n}"),
            ChatMessage(role="assistant", content="", tool_calls=(call,)),
            ChatMessage(role="tool", content="x" * 4_000, tool_call_id=f"t{n}"),
            ChatMessage(role="assistant", content=f"answer {n}"),
        ]
    roomy = build_window(history, token_budget=1_000_000)
    tight = build_window(history, token_budget=2_500)
    elided = [
        n
        for n in range(4)
        if any(m.content == ELIDED_MARKER for m in tight if m.tool_call_id == f"t{n}")
    ]
    kept = [m for m in tight if m.role == "tool" and m.content != ELIDED_MARKER]
    # The current turn and the one before it keep their results (2 kept turns).
    return tuple(history) == roomy and elided == [0, 1] and len(kept) == 2


def _self_test_replay() -> bool:
    """Replay a fixture session stored in a throwaway DB."""
    import tempfile
    from decimal import Decimal

    from whygraph.core import Config
    from whygraph.core.context import ProjectContext, use_project
    from whygraph.db import bootstrap, get_session
    from whygraph.db import engine as db_engine
    from whygraph.db.models import ChatMessage as Row
    from whygraph.db.models import ChatSession as Session
    from whygraph.portal.prices import Price
    from whygraph.services.llm.chat import ChatMessage

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        context = ProjectContext(
            slug="selftest", root=root, config=Config(whygraph_db=root / "wg.db")
        )
        db_engine._reset_engine()
        try:
            with use_project(context):
                bootstrap.ensure_initialized()
                now = "2026-10-30T00:00:00+00:00"
                with get_session() as db:
                    db.add(
                        Session(
                            id=1,
                            title="t",
                            provider="p",
                            model="m",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    db.flush()

                    def add(role, content, parent, calls="[]", call_id=None):
                        row = Row(
                            session_id=1,
                            role=role,
                            content=content,
                            tool_calls=calls,
                            tool_call_id=call_id,
                            parent_id=parent,
                            created_at=now,
                        )
                        db.add(row)
                        db.flush()
                        return row.id

                    def call(n):
                        return json.dumps(
                            [{"id": f"c{n}", "name": "get_evidence", "arguments": {}}]
                        )

                    last = add("user", "q1", None)
                    for n in (1, 2):
                        last = add("assistant", "", last, call(n))
                        last = add("tool", "r" * 4_000, last, call_id=f"c{n}")
                    last = add("assistant", "answer one", last)
                    last = add("user", "q2", last)
                    add("assistant", "answer two", last)
                system = (
                    ChatMessage(role="system", content="s" * 400),
                    ChatMessage(role="system", content="o" * 400),
                )
                price = Price(Decimal(2), Decimal(10), Decimal("0.1"), Decimal("2.5"))
                turns = _replay_session(
                    1, None, budget=500, price=price, system=system, specs_tokens=100
                )
        finally:
            db_engine._reset_engine()
    counts = [len(t["requests"]) for t in turns]
    first, second = turns[0]["requests"], turns[1]["requests"]
    return (
        counts == [3, 1]
        and first[0]["cached_tokens"] == 0
        and first[1]["cached_tokens"] >= first[1]["fixed_tokens"]
        and first[1]["modelled_usd"] > 0
        # Round 3 elided the older result in place: smaller than the naive growth.
        and first[2]["est_tokens"] < first[1]["est_tokens"] + 1_000
        and second[0]["est_tokens"] > 0
    )


def _self_test_judge() -> bool:
    """Vote parser, ``--compare`` packets, per-family tally, both refusals."""
    import tempfile
    from types import SimpleNamespace

    global LlmClientFactory
    ok = True
    vote = _parse_vote(
        'noise {"packet": "x", "winner": "B", "reason": "r"} ', "p001", "openai"
    )
    ok &= vote == {
        "packet": "p001",
        "winner": "B",
        "reason": "r",
        "judge_family": "openai",
    }
    ok &= _parse_vote("no json", "p001", "openai") is None
    ok &= _parse_vote('{"winner": "C"}', "p001", "openai") is None
    ok &= _judge_family("openrouter", "openai/gpt-x") == "openai"
    ok &= _judge_family("openai", "gpt-x") == "openai"

    def report(tag):
        return {
            "runs": [
                {
                    "suite": "s",
                    "mode": "s",
                    "trials": [
                        {
                            "case": c,
                            "question": f"q {c}",
                            "answer": f"{tag} answer {c}",
                            "tool_calls": [],
                            "error": "",
                        }
                        for c in ("C1", "C2", "C3")
                    ],
                }
            ]
        }

    with tempfile.TemporaryDirectory() as tmp:
        base, cand, out = (Path(tmp) / n for n in ("b.json", "c.json", "out"))
        base.write_text(json.dumps(report("old")))
        cand.write_text(json.dumps(report("new")))
        count = _write_compare_packets(str(base), str(cand), str(out))
        key = json.loads((out / "key.json").read_text())
        ok &= count == 6 and len(key) == 6
        ok &= all(
            word not in p.read_text()
            for p in out.glob("p*.json")
            for word in ("baseline", "candidate")
        )
        # Anthropic: candidate wins C1 and C2 (both orders), loses C3.
        # OpenAI: candidate wins C1, ties C2 (split), loses C3.
        want = {
            "anthropic": {"C1": "candidate", "C2": "candidate", "C3": "baseline"},
            "openai": {"C1": "candidate", "C2": "split", "C3": "baseline"},
        }
        seen: Counter = Counter()
        for packet_id, info in key.items():
            for family, plan in want.items():
                target = plan[info["question_id"]]
                if target == "split":
                    seen[(family, info["question_id"])] += 1
                    target = (
                        "candidate"
                        if seen[(family, info["question_id"])] == 1
                        else "baseline"
                    )
                winner = "A" if info["A"] == target else "B"
                (out / "votes" / f"{packet_id}-{family}.json").write_text(
                    json.dumps(
                        {
                            "packet": packet_id,
                            "winner": winner,
                            "reason": "t",
                            "judge_family": family,
                        }
                    )
                )
        table = _tally_votes(str(out))
        rows = {
            tuple(c.strip() for c in line.split("|")[1:-1])
            for line in table.splitlines()
            if line.startswith("| s ")
        }
        ok &= ("s", "anthropic", "2", "0", "1") in rows
        ok &= ("s", "openai", "1", "1", "1") in rows
        ok &= ("s", "pooled", "2", "0", "1") in rows
        # Both families agree on C1 and C3 (4 packets of 6); C2 differs on one order.
        ok &= "agreement (packets judged by 2+ families): 5/6" in table

        class _Forbidden:
            def __init__(self, *a, **k):
                raise AssertionError("LlmClientFactory was constructed")

        real, LlmClientFactory = LlmClientFactory, _Forbidden
        try:
            # Unpriced model, no operator prices: refused before any client.
            ok &= _judge_run("openai:not-a-priced-model", str(out), 5.0) == 2
            # Priced by the operator but the estimate is over the cap.
            ok &= (
                _judge_run(
                    "openai:not-a-priced-model",
                    str(out),
                    0.0001,
                    price_in=10.0,
                    price_out=40.0,
                )
                == 2
            )
        finally:
            LlmClientFactory = real
        # An in-cap run with a stand-in client writes votes and a report.
        calls = []

        class _Client:
            def complete(self, request):
                calls.append(request)
                return SimpleNamespace(
                    text='{"packet": "z", "winner": "A", "reason": "ok"}',
                    cost_usd=None,
                    input_tokens=1000,
                    output_tokens=50,
                )

        class _Factory:
            def __init__(self, config):
                pass

            def make(self, provider, *, model=None):
                return _Client()

        LlmClientFactory = _Factory
        try:
            for stale in (out / "votes").glob("*.json"):
                stale.unlink()
            code = _judge_run(
                "openai:not-a-priced-model", str(out), 5.0, price_in=1.0, price_out=2.0
            )
        finally:
            LlmClientFactory = real
        ok &= code == 0 and len(calls) == 6
        votes = list((out / "votes").glob("*.json"))
        ok &= len(votes) == 6
        ok &= all(json.loads(v.read_text())["judge_family"] == "openai" for v in votes)
    return bool(ok)


def _self_test() -> int:
    """Run both judge halves on an inline fake report; exit 0 on the expected table."""
    import tempfile

    def trial(case, mode_tag):
        return {
            "case": case,
            "question": f"question {case}",
            "trial": 1,
            "answer": f"answer {case} number {len(mode_tag)}",
            "tool_calls": [{"name": "t", "arguments": {"q": case}}],
            "error": "",
        }

    cases = ["X1", "X2", "X3", "X4"]
    report = {
        "runs": [
            {"suite": "lead", "mode": m, "trials": [trial(c, m) for c in cases]}
            for m in ("lead", "engineer", "onboarding")
        ]
    }
    # Outcome of lead vs engineer and vs onboarding for each question.
    outcome = {
        ("X1", "engineer"): "own",
        ("X1", "onboarding"): "own",
        ("X2", "engineer"): "split",
        ("X2", "onboarding"): "own",
        ("X3", "engineer"): "other",
        ("X3", "onboarding"): "own",
        ("X4", "engineer"): "split",
        ("X4", "onboarding"): "split",
    }
    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "report.json"
        report_path.write_text(json.dumps(report))
        out = Path(tmp) / "packets"
        count = _write_packets(str(report_path), str(out))
        key = json.loads((out / "key.json").read_text())
        ok = count == 16 and len(key) == 16
        ok &= all(
            "lead" not in json.dumps(json.loads(p.read_text()))
            for p in out.glob("p*.json")
        )
        votes = out / "votes"
        for packet_id, info in key.items():
            want = outcome[
                (info["question_id"], info["B"] if info["A"] == "lead" else info["A"])
            ]
            own_side = "A" if info["A"] == "lead" else "B"
            other_side = "B" if own_side == "A" else "A"
            if want == "own":
                picks = [own_side] * 3
            elif want == "other":
                picks = [other_side] * 3
            else:  # 3-3 across the two orders
                picks = [own_side, own_side, other_side]
                if own_side == "B":
                    picks = [other_side, other_side, own_side]
            (votes / f"{packet_id}.json").write_text(
                json.dumps(
                    [{"packet": packet_id, "winner": w, "reason": "t"} for w in picks]
                )
            )
        table = _tally_votes(str(out))
    row = next(line for line in table.splitlines() if line.startswith("| lead "))
    # X1 and X2 win; X3 loses to engineer; X4 ties both without a win.
    ok &= row.split("|")[2].strip() == "2" and row.split("|")[3].strip() == "4"
    print(table)
    for name, part in (
        ("refs and misuse", _self_test_refs),
        ("notes", _self_test_notes),
        ("multi-turn", _self_test_multi_turn),
        ("window elision", _self_test_window),
        ("replay", _self_test_replay),
        ("judge", _self_test_judge),
    ):
        passed = part()
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok &= passed
    print("self-test:", "OK" if ok else "FAILED")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default="http://127.0.0.1:8321",
        help="Portal URL. Keep the 127.0.0.1 host: the portal's Host guard "
        "rejects anything else.",
    )
    parser.add_argument(
        "--slug",
        default=os.path.basename(os.getcwd()),
        help="The project's slug in the portal (default: this directory's name).",
    )
    parser.add_argument(
        "--target",
        action="append",
        help="provider or provider:model. Repeatable.",
    )
    parser.add_argument(
        "--all-configured",
        action="store_true",
        help="Every provider the server reports as configured.",
    )
    parser.add_argument("--provider", default=None, help="Override [chat].provider.")
    parser.add_argument("--model", default=None)
    parser.add_argument(
        "--suite",
        action="append",
        help="A JSON suite (tests/fixtures/chat_eval/*.json). Repeatable. Switches "
        "to k-trial scoring and a Markdown report.",
    )
    parser.add_argument("--mode", default=None, help="Chat mode; unset sends nothing.")
    parser.add_argument("-k", type=int, default=3, help="Trials per case (suites).")
    parser.add_argument(
        "--suite-k",
        action="append",
        default=[],
        metavar="NAME=N",
        help="Trials for one suite, overriding -k. Repeatable.",
    )
    parser.add_argument(
        "--stop-above",
        type=float,
        default=None,
        metavar="USD",
        help="Stop before any trial that could push the org's month spend above "
        "this (checked against GET /api/budgets).",
    )
    parser.add_argument(
        "--warmup",
        action="store_true",
        help="One discarded k=1 pass first, so on-read backfill is not scored.",
    )
    parser.add_argument(
        "--report",
        default="eval-report.md",
        help="Markdown report path; the JSON transcript sidecar sits beside it.",
    )
    parser.add_argument(
        "--judge-packets",
        nargs=2,
        metavar=("REPORT_JSON", "DIR"),
        help="Write blind judge packets and key.json from a transcript JSON.",
    )
    parser.add_argument(
        "--compare",
        nargs=3,
        metavar=("BASELINE_JSON", "CANDIDATE_JSON", "DIR"),
        help="Write blind baseline-versus-candidate packets and key.json.",
    )
    parser.add_argument(
        "--judge-run",
        nargs=2,
        metavar=("PROVIDER:MODEL", "DIR"),
        help="Judge DIR's packets with a model (keys from the environment); "
        "writes DIR/votes. Needs --judge-max-usd.",
    )
    parser.add_argument("--judge-max-usd", type=float, default=None, metavar="USD")
    parser.add_argument(
        "--judge-price-in",
        type=float,
        default=None,
        metavar="USD",
        help="USD per million input tokens, required when the model is unpriced.",
    )
    parser.add_argument("--judge-price-out", type=float, default=None, metavar="USD")
    parser.add_argument(
        "--judge-tally",
        metavar="DIR",
        help="Tally DIR/votes/*.json against DIR/key.json into a Markdown table.",
    )
    parser.add_argument(
        "--replay",
        metavar="REPORT_JSON",
        help="Rebuild every request of a report's sessions in-process (no model "
        "calls) and write per-request estimated tokens and modelled cost.",
    )
    parser.add_argument("--data-dir", metavar="DIR", help="--replay: portal data dir.")
    parser.add_argument("--out", metavar="REPLAY_JSON", help="--replay: output file.")
    parser.add_argument(
        "--context-budget",
        type=int,
        default=None,
        metavar="N",
        help="--replay: [chat].context_token_budget (default: ChatConfig's).",
    )
    parser.add_argument(
        "--overview-from",
        metavar="REPLAY_JSON",
        help="--replay: reuse the overview text a previous replay recorded.",
    )
    parser.add_argument(
        "--cleanup-notes",
        action="store_true",
        help="Retire every '[eval] ' note of --slug (what a suite's setup.notes "
        "created) and exit. Uses the portal's notes routes; no provider key.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Exercise --judge-packets and --judge-tally on a fake report.",
    )
    args = parser.parse_args()

    if args.self_test:
        return _self_test()
    if args.cleanup_notes:
        try:
            return _cleanup_notes(args)
        except (urllib.error.URLError, OSError) as exc:
            print(f"! cannot reach {args.base}: {exc}", file=sys.stderr)
            return 2
    if args.replay:
        if not (args.data_dir and args.out):
            parser.error("--replay needs --data-dir and --out")
        return _replay_main(args)
    if args.judge_packets:
        _write_packets(*args.judge_packets)
        return 0
    if args.compare:
        _write_compare_packets(*args.compare)
        return 0
    if args.judge_run:
        if args.judge_max_usd is None:
            parser.error("--judge-run needs --judge-max-usd")
        return _judge_run(
            *args.judge_run,
            args.judge_max_usd,
            args.judge_price_in,
            args.judge_price_out,
        )
    if args.judge_tally:
        table = _tally_votes(args.judge_tally)
        Path(args.judge_tally, "tally.md").write_text(table + "\n")
        print(table)
        return 0

    try:
        targets = _parse_targets(args)
    except (urllib.error.URLError, OSError) as exc:
        print(f"! cannot reach {args.base}: {exc}", file=sys.stderr)
        print(
            "  start a dev portal first: uv run whygraph portal --data "
            '"${XDG_STATE_HOME:-$HOME/.local/state}/whygraph-dev" --port 8321'
        )
        return 2

    if args.suite:
        try:
            return _suite_main(args, targets)
        except (urllib.error.URLError, OSError) as exc:
            print(f"! portal error: {exc}", file=sys.stderr)
            return 2

    summary: list[tuple[str, str, int, int, list[tuple[str, bool]]]] = []
    for provider, model in targets:
        label = f"{provider or '(config default)'}{':' + model if model else ''}"
        try:
            rows = _run_target(_project_api(args), provider, model)
        except (urllib.error.URLError, OSError) as exc:
            print(f"! cannot reach {args.base}: {exc}", file=sys.stderr)
            return 2

        blocked = _all_failed(rows)
        if blocked:
            print(f"\n=== {label} === SKIPPED — every turn failed")
            print(f"    {blocked}")
            summary.append((label, blocked, 0, 0, []))
            continue
        passes, leaks = _report(label, rows)
        summary.append((label, "", passes, leaks, _clauses(rows)))

    if len(summary) > 1:
        print("\n=== cross-target summary ===")
        for label, blocked, passes, leaks, clauses in summary:
            if blocked:
                print(f"{label:28} SKIPPED  ({blocked[:48]})")
                continue
            marks = "".join("P" if ok else "F" for _, ok in clauses)
            print(
                f"{label:28} first-tool {passes}/{len(CASES)}  "
                f"leaks {leaks}  AC14 clauses {marks}"
            )
        print("\nA disagreement between rows is the finding — tool choice is")
        print("per-model, so one row proves nothing about another.")

    scored = [row for row in summary if not row[1]]
    if not scored:
        print("\n! no target was reachable — this run is NOT evidence of anything")
        return 2
    ok = all(passes == len(CASES) and leaks == 0 for _, _, passes, leaks, _ in scored)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
