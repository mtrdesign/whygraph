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
Start the playground in one shell::

    uv run whygraph serve --port 8321

Then, from the repository root::

    # every provider the server reports as configured
    SSL_CERT_FILE=/etc/ssl/cert.pem \\
      uv run python scripts/eval_tool_choice.py --all-configured

    # or specific targets, `provider` or `provider:model`
    uv run python scripts/eval_tool_choice.py \\
      --target deepseek:deepseek-v4-flash --target deepseek:deepseek-v4-pro

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
import sys
import urllib.error
import urllib.request

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
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310 -- localhost only
        return json.loads(response.read())


def _stream_tool_calls(
    base: str, session_id: int, question: str
) -> tuple[list, list, str]:
    """Send one turn and return its tool names, the full calls, and any error.

    The **arguments** matter as much as the names now: a chart case has to check
    that `render_chart` named columns rather than retyping values, and the
    identity case has to check the emitted SQL resolved identity through the
    `author` table. Neither is visible in a list of tool names.
    """
    body = json.dumps({"content": question}).encode()
    request = urllib.request.Request(
        f"{base}/api/chat/sessions/{session_id}/messages",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    names: list[str] = []
    calls: list[dict] = []
    error = ""
    with urllib.request.urlopen(request) as response:  # noqa: S310 -- localhost only
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            frame = json.loads(line[5:].strip())
            if frame.get("type") == "tool_call":
                names.append(frame["name"])
                calls.append(
                    {"name": frame["name"], "arguments": frame.get("arguments") or {}}
                )
            elif frame.get("type") == "error":
                error = frame.get("message", "unknown error")
    return names, calls, error


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
        session = _post(f"{base}/api/chat/sessions", session_body or None)
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
        providers = _post_get(f"{args.base}/api/chat/providers")
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
    with urllib.request.urlopen(url) as response:  # noqa: S310 -- localhost only
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8321")
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
    args = parser.parse_args()

    try:
        targets = _parse_targets(args)
    except (urllib.error.URLError, OSError) as exc:
        print(f"! cannot reach {args.base}: {exc}", file=sys.stderr)
        print("  start the server first: uv run whygraph serve --port 8321")
        return 2

    summary: list[tuple[str, str, int, int, list[tuple[str, bool]]]] = []
    for provider, model in targets:
        label = f"{provider or '(config default)'}{':' + model if model else ''}"
        try:
            rows = _run_target(args.base, provider, model)
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
