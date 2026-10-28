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
      uv run whygraph portal --data "${TMPDIR:-/tmp}/whygraph-dev" --port 8321

Then, from the repository root (``--slug`` is the project's slug in the
portal; it defaults to this checkout's directory name)::

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
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

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
            elif kind == "text_delta":
                turn["answer"] += frame.get("text", "")
            elif kind == "error":
                turn["error"] = frame.get("message", "unknown error")
            elif kind == "done":
                turn["input_tokens"] = frame.get("input_tokens")
                turn["output_tokens"] = frame.get("output_tokens")
    turn["latency"] = time.monotonic() - started
    return turn


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


def _check_case(expect: dict, turn: dict) -> list[str]:
    """The failed expectations of one trial; empty means the trial passed.

    ``tools_any``: at least one of these was called. ``tools_never``: none was.
    ``args``: ``{tool: {key: regex}}``, every named tool call must have the key
    matching (``re.search``) in at least one of its calls. ``cites``: regexes the
    final answer must match. ``forbid``: regexes the final answer must not
    match (the never-rank cases). ``max_rounds``: tool rounds at most this many.
    """
    failures: list[str] = []
    if turn["error"]:
        failures.append(f"turn error: {turn['error'][:60]}")
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


def _turn_cost(args, session_id: int) -> float | None:
    """The turn's spend from ``GET /api/usage/calls``, or None if unavailable.

    Usage rows are written by a background thread, so poll until the sum is
    stable across two reads.
    """
    url = (
        f"{args.base.rstrip('/')}/api/usage/calls"
        f"?project={args.slug}&chat_session={session_id}"
    )
    total: float | None = None
    for _ in range(12):
        time.sleep(0.5)
        try:
            items = _get_json(url).get("items", [])
        except (urllib.error.URLError, OSError, ValueError):
            return None
        seen = sum(item.get("cost_usd") or 0.0 for item in items) if items else None
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
    for case in suite["cases"]:
        for trial in range(1, k + 1):
            if args.stop_above is not None and (
                _spent(args) + TURN_COST_MARGIN > args.stop_above
            ):
                print(
                    f"! org spend would cross {args.stop_above}: stopping before "
                    f"{case['id']} t{trial}",
                    file=sys.stderr,
                )
                return trials
            session = _post(f"{_project_api(args)}/chat/sessions", session_body or None)
            turn = _stream_turn(_project_api(args), session["id"], case["question"])
            failures = _check_case(case.get("expect") or {}, turn)
            turn.update(
                case=case["id"],
                question=case["question"],
                trial=trial,
                session_id=session["id"],
                failures=failures,
                passed=not failures,
                cost=_turn_cost(args, session["id"]),
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
        "tokens": _mean(
            [(t["input_tokens"] or 0) + (t["output_tokens"] or 0) for t in trials]
        ),
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
            f"- Mean tokens (in+out, last call): {summ['tokens']:.0f}",
            f"- Mean latency: {summ['latency']:.1f}s",
            f"- Tool-error rate: {summ['tool_error_rate']:.1%} of tool calls",
            f"- Cost per turn: ${summ['cost']:.4f} (total ${summ['cost_total']:.2f})",
            "- Tool counts: "
            + ", ".join(f"{n} {c}" for n, c in summ["tools"].most_common()),
            "",
            "| case | passed | pass^k | rounds | tokens | latency | cost | failures |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for case_id, ts in summ["by_case"].items():
            one = _summarize(ts)
            fails = "; ".join(sorted({f for t in ts for f in t["failures"]}))
            out.append(
                f"| {case_id} | {sum(t['passed'] for t in ts)}/{len(ts)} "
                f"| {'yes' if one['pass_k'] else 'no'} | {one['rounds']:.1f} "
                f"| {one['tokens']:.0f} | {one['latency']:.0f}s "
                f"| ${one['cost']:.3f} | {fails or '-'} |"
            )
        out.append("")
    return "\n".join(out)


def _suite_main(args, targets: list[tuple[str | None, str | None]]) -> int:
    """Run ``--suite`` files over each target; write the report and sidecar."""
    suites = [_load_suite(path) for path in args.suite]
    suite_k = {name: int(n) for name, _, n in (a.partition("=") for a in args.suite_k)}
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
        "--judge-tally",
        metavar="DIR",
        help="Tally DIR/votes/*.json against DIR/key.json into a Markdown table.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Exercise --judge-packets and --judge-tally on a fake report.",
    )
    args = parser.parse_args()

    if args.self_test:
        return _self_test()
    if args.judge_packets:
        _write_packets(*args.judge_packets)
        return 0
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
            '"${TMPDIR:-/tmp}/whygraph-dev" --port 8321'
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
