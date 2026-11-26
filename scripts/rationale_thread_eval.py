"""Plan 62 Part A eval (section 9): review threads in the rationale card.

Measures what the review-thread pass (plan 62 section 4.9 / 4.9a) does to a
rationale card. Every target's evidence and review scope are built exactly
as the product builds them (``collect_evidence``, the lazy description
backfill, ``review_scope``, the generator's ``paths`` / ``file_lines`` /
``target_range``); the baseline is today's generator with ``paths=None``,
run twice for a noise floor, and the treated arm adds the thread inputs.

Everything runs against **copies** of the project's databases (the WhyGraph
DB from the portal's data directory and the repository's CodeGraph DB), so a
portal restart cannot disturb it and nothing the portal holds is written.
The provider key and the project config come from the portal's own
Postgres through :func:`whygraph.portal.context` (the same code that builds
a request's context). Every paid call is metered twice: into the eval's
ledger (``plans/62-eval/spend.jsonl``, priced with the portal's price
table) and into the portal's own usage ledger as a ``scan`` row of the
project (``--no-portal-ledger`` skips the second). The ledger's cumulative
total is checked before every paid call; the run stops cleanly before the
cap.

Deliberately **not** pytest: it calls a paid provider and its results vary.

Usage
-----
The sub-commands run in order; each writes what the next one reads::

    uv run --no-sync python scripts/rationale_thread_eval.py copy --v1-db OLD.db
    uv run --no-sync python scripts/rationale_thread_eval.py survey
    uv run --no-sync python scripts/rationale_thread_eval.py select \\
        --adversarial plans/62-eval/part-a-adversarial.json
    uv run --no-sync python scripts/rationale_thread_eval.py generate --yes
    uv run --no-sync python scripts/rationale_thread_eval.py prompts
    uv run --no-sync python scripts/rationale_thread_eval.py measure

``generate`` without ``--yes`` prints the corpus and the estimate only.

Notes
-----
* The defaults point at ``make dev-local`` (Postgres on ``127.0.0.1:55432``,
  data in ``~/.local/state/whygraph-dev/local/data``).
* On a corporate network the LLM SDKs may need ``SSL_CERT_FILE`` set (or
  unset) for the provider's certificate chain.
* M4 and M6 are judged by hand: ``measure`` writes their sheets and never
  judges them.
"""

from __future__ import annotations

import argparse
import contextvars
import difflib
import importlib.util
import json
import os
import random
import re
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO / "plans" / "62-eval"
WORK = EVAL_DIR / "part-a-work"
CARDS = EVAL_DIR / "part-a-cards"
LEDGER = EVAL_DIR / "spend.jsonl"

DEFAULT_DATA = Path("~/.local/state/whygraph-dev/local/data").expanduser()
DEFAULT_DB_URL = (
    "postgresql+psycopg://whygraph:whygraph-dev@127.0.0.1:55432/whygraph"  # dev only
)
MODEL = "claude-haiku-5-5"
PROVIDER = "anthropic"
CAP_USD = 10.0
RESERVE_USD = 0.25
"""Stop a paid call when the ledger is this close to the cap."""

ARMS = ("baseline1", "baseline2", "treated")
PRE_PLAN_REV = "b466670"
"""The commit before plan 62 (``main`` at writing): the pre-plan generator."""

CORPUS_SIZES = {"thread": 15, "filtered": 3, "stale": 3, "control": 5}
MIN_SYMBOL_MULTI = 5
MIN_ADVERSARIAL = 3
MAX_SPAN = 400
"""Symbols longer than this many lines are not targets (a card per module)."""

FUZZY_SAME = 0.6
"""``SequenceMatcher`` ratio at or above which two card sentences are the same."""

_CURRENT_CALL: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "eval_call", default=("-", "-")
)


class BudgetStop(RuntimeError):
    """The next paid call would cross the cap."""


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class Ledger:
    """The shared spend ledger (``plans/62-eval/spend.jsonl``), thread-safe.

    Parameters
    ----------
    path : Path
        The JSONL file; created when missing. Every line holds a numeric
        ``cost``.
    cap : float
        The cumulative cap in USD across every line of the file.
    """

    def __init__(self, path: Path, cap: float) -> None:
        self.path = path
        self.cap = cap
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)

    def total(self) -> float:
        """The cumulative cost of every line, re-read from the file.

        Returns
        -------
        float
            The sum of the ``cost`` fields in USD.
        """
        total = 0.0
        with self._lock:
            for line in self.path.read_text().splitlines():
                if line.strip():
                    total += float(json.loads(line).get("cost") or 0.0)
        return total

    def check(self, upcoming: float) -> None:
        """Raise :class:`BudgetStop` when ``upcoming`` would cross the cap.

        Parameters
        ----------
        upcoming : float
            The estimated cost of the next call, in USD.

        Raises
        ------
        BudgetStop
            When the total plus ``upcoming`` plus :data:`RESERVE_USD`
            exceeds the cap.
        """
        spent = self.total()
        if spent + upcoming + RESERVE_USD > self.cap:
            raise BudgetStop(
                f"ledger at ${spent:.4f}; next call ~${upcoming:.4f} would cross "
                f"${self.cap:.2f} (reserve ${RESERVE_USD:.2f})"
            )

    def append(self, entry: dict) -> None:
        """Append one entry (a JSON object with a numeric ``cost``).

        Parameters
        ----------
        entry : dict
            The line to write.
        """
        with self._lock:
            with self.path.open("a") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class Env:
    """What every sub-command needs: paths, the project and its portal ids."""

    slug: str
    data: Path
    db_url: str
    root: Path | None = None
    project_id: int | None = None
    org_id: int | None = None
    project_name: str | None = None


def _set_env(env: Env) -> None:
    """Point the portal code at the dev portal's data dir and Postgres."""
    os.environ["WHYGRAPH_DATA"] = str(env.data)
    os.environ["WHYGRAPH_DATABASE_URL"] = env.db_url


def _project_merged(env: Env) -> tuple[dict, dict]:
    """The project's merged config with decrypted keys, from the portal DB.

    Returns ``(merged, key_scopes)``; fills ``env``'s ids and root. Uses the
    portal's own layer merge and secret injection
    (:mod:`whygraph.portal.context`).
    """
    _set_env(env)
    from sqlmodel import select

    from whygraph.portal import db as portal_db
    from whygraph.portal.context import _inject_secrets, _merged_layer, resolve_root
    from whygraph.portal.models import Project

    with portal_db.get_session() as session:
        rows = session.exec(select(Project).where(Project.slug == env.slug)).all()
        live = [r for r in rows if getattr(r, "deleted_at", None) is None]
        if len(live) != 1:
            raise SystemExit(f"expected one project {env.slug!r}, found {len(live)}")
        project = live[0]
        root = resolve_root(project)
        project_layer, merged = _merged_layer(session, project, root)
        key_scopes = _inject_secrets(session, project, project_layer, merged)
        env.root = root
        env.project_id = project.id
        env.org_id = project.org_id
        env.project_name = project.name
    return merged, key_scopes


def _portal_db_paths(env: Env) -> tuple[Path, Path]:
    """The portal's own (live) WhyGraph DB and the repo's CodeGraph DB."""
    merged, _ = _project_merged(env)
    return Path(merged["whygraph_db"]), Path(merged["codegraph_db"])


def eval_context(env: Env, whygraph_db: Path, codegraph_db: Path) -> Any:
    """A :class:`ProjectContext` on DB copies, with the eval model.

    The project's config and keys as the portal resolves them, then: the DB
    paths moved to the copies, the GitHub token dropped (nothing here calls
    GitHub), and ``analyze`` / ``rationale`` pinned to the eval model.

    Parameters
    ----------
    env : Env
        The environment.
    whygraph_db, codegraph_db : Path
        The copies to read.

    Returns
    -------
    ProjectContext
        The context to bind with ``use_project``.
    """
    from whygraph.core.config import Config
    from whygraph.core.context import ProjectContext

    merged, key_scopes = _project_merged(env)
    scan = merged.get("scan")
    if isinstance(scan, dict):
        scan.pop("token", None)
    merged["whygraph_db"] = str(whygraph_db)
    merged["codegraph_db"] = str(codegraph_db)
    for task in ("analyze", "rationale"):
        table = merged.setdefault(task, {})
        table.pop("provider", None)
        table["model"] = f"{PROVIDER}/{MODEL}"
    assert env.root is not None
    config = Config.from_dict(merged, env.root)
    for task in ("analyze", "rationale"):
        choice = tuple(config.model_for(task))
        if choice != (PROVIDER, MODEL):
            raise SystemExit(f"{task} resolves to {choice}, not {PROVIDER}/{MODEL}")
    if key_scopes.get(PROVIDER, "none") == "none":
        raise SystemExit(f"the portal stores no {PROVIDER} key for {env.slug!r}")
    return ProjectContext(
        slug=env.slug, root=env.root, config=config, key_scopes=key_scopes
    )


# ---------------------------------------------------------------------------
# Metering
# ---------------------------------------------------------------------------


class EvalSink:
    """A usage sink: the eval ledger, plus the portal's own usage ledger.

    Parameters
    ----------
    ledger : Ledger
        The eval ledger.
    config : Config
        The eval context's config (for pricing).
    portal : PortalUsageSink or None
        Forwarded to, so the portal's Usage page shows the eval's spend.
    """

    def __init__(self, ledger: Ledger, config: Any, portal: Any | None) -> None:
        from whygraph.core.usage import UsageScope

        self.scope = UsageScope(source="scan")
        self._ledger = ledger
        self._config = config
        self._portal = portal

    def record(self, rec: Any) -> None:
        """Price one call with the portal's table and write both ledgers.

        Parameters
        ----------
        rec : UsageRecord
            What the provider reported.
        """
        from whygraph.portal.prices import NO_OVERRIDES
        from whygraph.portal.usage import price_usage

        priced = price_usage(
            rec,
            provider=rec.provider,
            model_requested=rec.model_requested,
            overrides=NO_OVERRIDES,
            config=self._config,
        )
        if priced.cost_usd is None:
            raise SystemExit(f"unpriced call: {rec.provider}/{rec.model_requested}")
        target, arm = _CURRENT_CALL.get()
        self._ledger.append(
            {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "step": "plan62-part-a",
                "cost": float(priced.cost_usd),
                "provider": rec.provider,
                "model": rec.model_served or rec.model_requested,
                "task": rec.task,
                "target": target,
                "arm": arm,
                "input_tokens": rec.input_tokens,
                "output_tokens": rec.output_tokens,
            }
        )
        if self._portal is not None:
            try:
                self._portal.record(rec)
            except Exception as exc:  # noqa: BLE001 -- the eval ledger already has it
                print(f"  (portal ledger write failed: {exc})", file=sys.stderr)

    def blocked_scope(self) -> str | None:
        """Never blocks (the eval's cap is :meth:`Ledger.check`)."""
        return None


def _portal_sink(env: Env, config: Any, key_scopes: Any) -> tuple[Any, Any]:
    """A :class:`PortalUsageSink` writing ``scan`` rows of the project, and its writer."""
    from whygraph.core.usage import UsageScope
    from whygraph.portal.usage import Attribution, PortalUsageSink
    from whygraph.portal.usage_store import PriceBook, SpendBook, UsageWriter

    writer = UsageWriter()
    writer.start()
    attribution = Attribution(
        org_id=env.org_id,
        org_slug="local",
        project_id=env.project_id,
        project_slug=env.slug,
        project_name=env.project_name or env.slug,
        actor_kind="system",
        user_id=None,
        actor_label="System",
        config=config,
        key_scopes=key_scopes,
        client_name="plan62-eval",
    )
    sink = PortalUsageSink(
        UsageScope(source="scan"), attribution, writer, SpendBook(), PriceBook()
    )
    return sink, writer


class GuardedClient:
    """An :class:`LlmClient` proxy: checks the cap first, keeps the prompt.

    Parameters
    ----------
    inner : LlmClient
        The real client.
    ledger : Ledger
        Checked before the call.
    price_in, price_out : float
        USD per million tokens, for the pre-call estimate.
    max_out_tokens : int
        Output tokens assumed by the estimate.
    """

    def __init__(
        self,
        inner: Any,
        ledger: Ledger,
        price_in: float,
        price_out: float,
        max_out_tokens: int = 4000,
    ) -> None:
        self._inner = inner
        self._ledger = ledger
        self._price_in = price_in
        self._price_out = price_out
        self._max_out = max_out_tokens
        self.provider = inner.provider
        self.model = inner.model
        self.last_prompt: dict | None = None

    def preflight(self) -> None:
        """Forwarded."""
        self._inner.preflight()

    def complete(self, request: Any) -> Any:
        """Check the cap, record the prompt, then call the real client.

        Parameters
        ----------
        request : CompletionRequest
            The request.

        Returns
        -------
        CompletionResponse
            The real reply.
        """
        chars = sum(len(m.content) for m in request.messages)
        estimate = (
            chars / 3.0 * self._price_in + self._max_out * self._price_out
        ) / 1e6
        self._ledger.check(estimate)
        self.last_prompt = {m.role: m.content for m in request.messages}
        return self._inner.complete(request)


class PromptOnlyClient:
    """A client that keeps the prompt and answers an empty card (no spend)."""

    provider = PROVIDER
    model = MODEL

    def __init__(self) -> None:
        self.last_prompt: dict | None = None

    def preflight(self) -> None:
        """Nothing to check."""

    def complete(self, request: Any) -> Any:
        """Keep the prompt; return a minimal valid card.

        Parameters
        ----------
        request : CompletionRequest
            The request.

        Returns
        -------
        CompletionResponse
            A stub reply with no tokens (so nothing is metered).
        """
        from whygraph.services.llm.types import CompletionResponse

        self.last_prompt = {m.role: m.content for m in request.messages}
        text = json.dumps(
            {"purpose": "", "why": "", "constraints": [], "tradeoffs": [], "risks": []}
        )
        return CompletionResponse(text=text, model=MODEL, provider=PROVIDER)


def _price_per_m() -> tuple[float, float]:
    """The eval model's input / output price per million tokens."""
    from whygraph.portal.prices import NO_OVERRIDES, price_for

    hit = price_for(PROVIDER, MODEL, NO_OVERRIDES, custom_endpoint=False)
    if hit is None:
        raise SystemExit(f"{PROVIDER}/{MODEL} is not in the price table")
    price, _ = hit
    return float(price.input), float(price.output)


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------


def _sqlite_copy(src: Path, dst: Path) -> None:
    """Copy a SQLite DB consistently (the backup API; WAL content included)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        source.execute("select 1 from sqlite_master").fetchone()
    except sqlite3.OperationalError:
        # A read-only open fails on a WAL DB whose -shm is stale; a static copy
        # nobody holds can be opened normally.
        source = sqlite3.connect(src)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()


def cmd_copy(env: Env, args: argparse.Namespace) -> None:
    """Copy the project's DBs (and optionally a v1 DB) into the work dir."""
    wg, cg = _portal_db_paths(env)
    _sqlite_copy(wg, WORK / "v2" / "whygraph.db")
    _sqlite_copy(cg, WORK / "cg" / "codegraph.db")
    print(f"copied {wg} and {cg} into {WORK}")
    if args.v1_db:
        v1 = WORK / "v1" / "whygraph.db"
        _sqlite_copy(Path(args.v1_db).expanduser(), v1)
        from whygraph.core.context import use_project
        from whygraph.db.bootstrap import ensure_initialized

        with use_project(eval_context(env, v1, WORK / "cg" / "codegraph.db")):
            ensure_initialized()
        print(f"copied and migrated the v1 DB into {v1}")
    con = sqlite3.connect(WORK / "v2" / "whygraph.db")
    total, threaded, synced = con.execute(
        "select count(*), sum(coalesce(review_threads_total,0) > 0), "
        "sum(review_threads_total > 0 and review_threads_synced = 1) from pull_request"
    ).fetchone()
    con.close()
    print(f"PRs {total}, with threads {threaded}, thread-synced {synced}")


# ---------------------------------------------------------------------------
# survey
# ---------------------------------------------------------------------------


@dataclass
class Node:
    """A CodeGraph function / method node."""

    id: str
    kind: str
    qualified_name: str
    path: str
    start: int
    end: int


def _load_nodes(cg: Path) -> tuple[dict[str, list[Node]], Counter]:
    """Function and method nodes per file, and how often each qualified name occurs."""
    con = sqlite3.connect(f"file:{cg}?mode=ro", uri=True)
    rows = con.execute(
        "select id, kind, qualified_name, file_path, start_line, end_line from nodes "
        "where kind in ('function', 'method')"
    ).fetchall()
    con.close()
    by_file: dict[str, list[Node]] = defaultdict(list)
    names: Counter = Counter()
    for row in rows:
        node = Node(*row)
        by_file[node.path].append(node)
        names[node.qualified_name] += 1
    return by_file, names


def _enclosing(nodes: list[Node], line: int, names: Counter) -> Node | None:
    """The innermost uniquely named function / method holding ``line``."""
    hits = [
        n
        for n in nodes
        if n.start <= line <= n.end
        and names[n.qualified_name] == 1
        and n.end - n.start + 1 <= MAX_SPAN
    ]
    return min(hits, key=lambda n: n.end - n.start) if hits else None


def _rules_1_2(thread: dict, pr: Any) -> bool:
    """Plan 62 section 4.9a rules 1 and 2, with the generator's own helpers."""
    from whygraph.analyze.rationale_generator import (
        _THREAD_AUTHOR_MIN_CHARS,
        _clean_thread_comment,
        _is_machine_text,
    )

    comments = [c for c in thread.get("comments") or () if isinstance(c, dict)]
    if not comments:
        return False
    opener = comments[0]
    if opener.get("bot") or opener.get("minimized"):
        return False
    if _is_machine_text(opener.get("body")):
        return False
    kept = []
    for c in comments:
        if c.get("bot") or c.get("minimized") or _is_machine_text(c.get("body")):
            continue
        text = _clean_thread_comment(c.get("body"))
        if text:
            kept.append((str(c.get("author") or ""), text))
    rendered = kept[:1] + kept[1:][-2:]
    author = (pr.author or "").lower()
    return bool(author) and any(
        who.lower() == author and len(text) >= _THREAD_AUTHOR_MIN_CHARS
        for who, text in rendered
    )


def _thread_key(number: int, index: int) -> str:
    return f"PR#{number}/t{index}"


def _evaluate(target: Any) -> dict:
    """Evidence, scope and per-thread rule results of one target (no LLM)."""
    from whygraph.analyze.rationale_generator import (
        _LINE_SOURCES,
        _anchor_owned,
        _json_list,
        _PrRenderCaps,
        _select_threads,
    )
    from whygraph.core.code_anchor import anchor_positions
    from whygraph.core import get_config
    from whygraph.db import get_session
    from whygraph.mcp.evidence import collect_evidence, review_scope
    from whygraph.mcp.targets import repo_root

    evidence = collect_evidence(target, limit=20)
    with get_session() as session:
        scope = review_scope(session, repo_root(), target.path)
    rng = (target.line_start, target.line_end)
    caps = _PrRenderCaps.from_config(get_config().rationale)
    selected = _select_threads(evidence, scope.paths, scope.current_lines, rng, caps)
    line_linked = {
        pr.number
        for item in evidence
        if item.source in _LINE_SOURCES
        for pr in item.pull_requests
    }
    blame = [i for i in evidence if i.source == "blame" and i.lines]
    prs: dict[int, Any] = {}
    for item in evidence:
        for pr in item.pull_requests:
            prs.setdefault(pr.number, pr)
    threads = []
    for number, pr in prs.items():
        for idx, raw in enumerate(_json_list(pr.review_threads)):
            if not isinstance(raw, dict) or raw.get("path") not in scope.paths:
                continue
            anchor = raw.get("anchor") if isinstance(raw.get("anchor"), dict) else None
            positions = (
                anchor_positions(anchor.get("lines"), scope.current_lines)
                if anchor and scope.current_lines
                else []
            )
            in_range = [p for p in positions if rng[0] <= p <= rng[1]]
            owned = bool(
                scope.current_lines is not None
                and _anchor_owned(raw, pr, blame, scope.current_lines, rng)
            )
            threads.append(
                {
                    "key": _thread_key(number, idx),
                    "pr": number,
                    "rules_1_2": _rules_1_2(raw, pr),
                    "rule_3": number in line_linked,
                    "rule_4": owned,
                    "anchor": anchor is not None,
                    "added": bool(anchor and anchor.get("added")),
                    "positions": positions,
                    "in_range": in_range,
                    "comments": len(raw.get("comments") or []),
                    "comments_total": raw.get("comments_total"),
                }
            )
    return {
        "evidence": evidence,
        "scope": scope,
        "selected": selected,
        "threads": threads,
        "line_linked": sorted(line_linked),
        "sources": dict(Counter(i.source for i in evidence)),
    }


def _classify(result: dict, file_has_threads: bool) -> tuple[str, str]:
    """``(class, reason)`` of one evaluated target (plan 62 section 9)."""
    threads = result["threads"]
    if result["selected"]:
        return "thread", "a section 4.9a-qualified thread renders"
    near = [t for t in threads if t["rules_1_2"] and t["rule_3"]]
    if near:
        t = near[0]
        if not t["anchor"]:
            why = "no anchor"
        elif not t["positions"]:
            why = "anchor gone from the file"
        elif not t["in_range"]:
            why = "anchor outside the target range"
        elif len(t["positions"]) > 1:
            why = "anchor duplicated in the file, the copy in range not owned (R22)"
        else:
            why = "anchor in range but not owned by the PR (R22)"
        return "stale", f"{t['key']} passes rules 1-3; {why}"
    if threads:
        return (
            "filtered",
            f"{len(threads)} bundle thread(s) on the file, none qualifies",
        )
    if file_has_threads:
        return (
            "filtered-outside-bundle",
            "threads on the file, none in the bundle's PRs",
        )
    return "control", "no stored thread on the file"


def _target_of(node: Node) -> Any:
    from whygraph.mcp.targets import Target

    return Target(node.path, node.start, node.end, node.qualified_name)


def _pr_rows(db: Path) -> list[tuple[int, str, str, list]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select number, author, created_at, review_threads from pull_request "
        "where review_threads is not null and review_threads != '[]'"
    ).fetchall()
    con.close()
    return [(n, a, c, json.loads(t)) for n, a, c, t in rows]


def cmd_survey(env: Env, args: argparse.Namespace) -> None:
    """Classify candidate targets mechanically and write ``part-a-survey.json``."""
    from types import SimpleNamespace

    from whygraph.core.code_anchor import anchor_positions
    from whygraph.core.context import use_project
    from whygraph.db import get_session
    from whygraph.mcp.evidence import review_scope

    v2, cg = WORK / "v2" / "whygraph.db", WORK / "cg" / "codegraph.db"
    ctx = eval_context(env, v2, cg)
    by_file, names = _load_nodes(cg)
    prs = _pr_rows(v2)
    threaded_paths = {
        t.get("path") for _, _, _, ts in prs for t in ts if isinstance(t, dict)
    }
    rnd = random.Random(args.seed)
    # Candidate symbols, by the kind of thread that points at them.
    pools: dict[str, list[tuple[Node, str]]] = defaultdict(list)
    file_lines: dict[str, Any] = {}
    with use_project(ctx):
        for number, author, created, threads in prs:
            pr = SimpleNamespace(author=author, created_at=created, number=number)
            for idx, thread in enumerate(threads):
                if not isinstance(thread, dict):
                    continue
                path = thread.get("path")
                if path not in by_file:
                    continue
                if path not in file_lines:
                    with get_session() as session:
                        file_lines[path] = review_scope(
                            session, env.root, path
                        ).current_lines
                lines = file_lines[path]
                anchor = thread.get("anchor")
                key = _thread_key(number, idx)
                if not lines or not isinstance(anchor, dict):
                    continue
                positions = anchor_positions(anchor.get("lines"), lines)
                good = _rules_1_2(thread, pr)
                if positions:
                    for y in positions:
                        node = _enclosing(by_file[path], y, names)
                        if node is None:
                            continue
                        pool = (
                            "dup"
                            if len(positions) > 1
                            else ("live" if good else "live-unqualified")
                        )
                        pools[pool].append((node, key))
                elif good and thread.get("original_line"):
                    node = _enclosing(by_file[path], thread["original_line"], names)
                    if node is not None:
                        pools["gone"].append((node, key))
        control_files = [
            p
            for p, nodes in by_file.items()
            if p not in threaded_paths
            and nodes
            and not p.startswith(("tests/", "test"))
        ]
        rnd.shuffle(control_files)
        for path in control_files[: args.control_files]:
            nodes = [n for n in by_file[path] if names[n.qualified_name] == 1]
            nodes = [n for n in nodes if 5 <= n.end - n.start + 1 <= MAX_SPAN]
            if nodes:
                pools["control"].append((rnd.choice(nodes), "-"))

        quota = {
            "live": args.live,
            "live-unqualified": args.unqualified,
            "gone": args.gone,
            "dup": args.dup,
            "control": args.control_files,
        }
        seen: dict[str, dict] = {}
        failures = 0
        for pool, items in pools.items():
            rnd.shuffle(items)
            done = 0
            for node, key in items:
                if done >= quota.get(pool, 0):
                    break
                if node.id in seen:
                    seen[node.id]["pools"].append(pool)
                    continue
                started = time.monotonic()
                try:
                    result = _evaluate(_target_of(node))
                except Exception as exc:  # noqa: BLE001 -- a survey row, not a failure
                    print(f"  skip {node.qualified_name}: {exc}", file=sys.stderr)
                    failures += 1
                    if failures > 25:
                        raise SystemExit("too many survey failures") from exc
                    continue
                done += 1
                cls, reason = _classify(result, node.path in threaded_paths)
                seen[node.id] = {
                    "id": node.id,
                    "qualified_name": node.qualified_name,
                    "kind": node.kind,
                    "path": node.path,
                    "start": node.start,
                    "end": node.end,
                    "pools": [pool],
                    "from_thread": key,
                    "class": cls,
                    "reason": reason,
                    "evidence_commits": len(result["evidence"]),
                    "sources": result["sources"],
                    "selected_prs": sorted(result["selected"]),
                    "threads": result["threads"],
                    "seconds": round(time.monotonic() - started, 2),
                }
                print(
                    f"  [{pool}] {cls:<24} {node.path}:{node.start}-{node.end} "
                    f"{node.qualified_name} ({seen[node.id]['seconds']}s)"
                )
    out = WORK / "survey.json"
    out.write_text(json.dumps(list(seen.values()), indent=1))
    print(f"wrote {out}: " + json.dumps(Counter(v["class"] for v in seen.values())))


# ---------------------------------------------------------------------------
# candidates (for the hand-picked adversarial targets)
# ---------------------------------------------------------------------------


def cmd_candidates(env: Env, args: argparse.Namespace) -> None:
    """Print the qualified threads of thread-class survey targets, for hand-picking."""
    v2 = WORK / "v2" / "whygraph.db"
    survey = json.loads((WORK / "survey.json").read_text())
    prs = {n: (a, ts) for n, a, _, ts in _pr_rows(v2)}
    for row in survey:
        if row["class"] != "thread":
            continue
        for t in row["threads"]:
            if not (t["rules_1_2"] and t["rule_3"] and t["rule_4"]):
                continue
            if t["comments"] < args.min_comments:
                continue
            author, threads = prs[t["pr"]]
            idx = int(t["key"].split("/t")[1])
            thread = threads[idx]
            print("=" * 78)
            print(f"{row['id']}  {row['qualified_name']}  {row['path']}")
            print(f"{t['key']} by PR author {author}; comments {t['comments']}")
            for c in thread["comments"]:
                body = re.sub(r"\s+", " ", c.get("body") or "")[: args.chars]
                print(f"  [{c.get('author')}] {body}")


# ---------------------------------------------------------------------------
# select
# ---------------------------------------------------------------------------


def _other_function_threads(row: dict) -> list[dict]:
    """Threads on the same file that pass rules 1-2 and sit on other code."""
    return [
        t
        for t in row["threads"]
        if t["rules_1_2"] and t["positions"] and not t["in_range"]
    ]


def cmd_select(env: Env, args: argparse.Namespace) -> None:
    """Pick the corpus from the survey; write ``part-a-corpus.json``."""
    survey = json.loads((WORK / "survey.json").read_text())
    picks = json.loads(Path(args.adversarial).read_text()) if args.adversarial else []
    rnd = random.Random(args.seed)
    by_id = {row["id"]: row for row in survey}
    chosen: list[dict] = []

    def take(row: dict, cls: str, how: str) -> None:
        if any(c["id"] == row["id"] for c in chosen):
            return
        chosen.append({**row, "corpus_class": cls, "chosen_by": how})

    for pick in picks:
        row = by_id[pick["id"]]
        if row["class"] != "thread":
            raise SystemExit(f"adversarial pick {pick['id']} is not a thread target")
        take(row, "thread", f"hand-picked adversarial: {pick['reason']}")
    threads = [r for r in survey if r["class"] == "thread"]
    rnd.shuffle(threads)
    multi = [r for r in threads if _other_function_threads(r)]
    # Prefer files where another function's thread is also line-linked here
    # (rule 3 holds in this bundle): only R21 / R22 keep it out.
    multi.sort(key=lambda r: not any(t["rule_3"] for t in _other_function_threads(r)))
    for row in multi:
        if sum(1 for c in chosen if _other_function_threads(c)) >= MIN_SYMBOL_MULTI:
            break
        take(row, "thread", "mechanical: symbol in a file with threads on other code")
    for row in threads:
        if (
            sum(1 for c in chosen if c["corpus_class"] == "thread")
            >= CORPUS_SIZES["thread"]
        ):
            break
        take(row, "thread", "mechanical: random thread target (seeded)")
    for cls in ("filtered", "stale", "control"):
        pool = [r for r in survey if r["class"] == cls]
        if cls == "filtered" and len(pool) < CORPUS_SIZES[cls]:
            pool += [r for r in survey if r["class"] == "filtered-outside-bundle"]
        if cls == "stale":
            # At least one duplicate-code case when the corpus has one.
            # Stale means an anchor that is gone, outside or not owned; a
            # thread with no anchor at all is a different (fail-closed) case.
            pool = [r for r in pool if "no anchor" not in r["reason"]]
            pool.sort(key=lambda r: "duplicated" not in r["reason"])
            head, rest = pool[:1], pool[1:]
            rnd.shuffle(rest)
            pool = head + rest
        else:
            rnd.shuffle(pool)
        pool = [r for r in pool if r["evidence_commits"] > 0]
        for row in pool[: CORPUS_SIZES[cls]]:
            take(row, cls, f"mechanical: {row['reason']}")
    counts = Counter(c["corpus_class"] for c in chosen)
    for cls, want in CORPUS_SIZES.items():
        if counts[cls] < want:
            print(f"WARNING: {cls} has {counts[cls]} of {want}", file=sys.stderr)
    for i, c in enumerate(chosen, 1):
        c["tid"] = f"T{i:02d}"
    out = EVAL_DIR / "part-a-corpus.json"
    out.write_text(json.dumps(chosen, indent=1))
    print(f"wrote {out}: {dict(counts)}")
    for c in chosen:
        print(
            f"  {c['tid']} {c['corpus_class']:<8} {c['path']}:{c['start']}-{c['end']} "
            f"{c['qualified_name']} - {c['chosen_by']}"
        )


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


def _item_dict(item: Any) -> dict:
    return {
        "text": item.text,
        "basis": item.basis,
        "refs": [r.as_dict() for r in item.refs],
        "quote": item.quote,
    }


def _card_dict(r: Any) -> dict:
    return {
        "purpose": r.purpose,
        "why": r.why,
        "constraints": [_item_dict(i) for i in r.constraints],
        "tradeoffs": list(r.tradeoffs),
        "risks": [_item_dict(i) for i in r.risks],
        "model": r.model,
        "provider": r.provider,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
    }


def _texts_by_origin(evidence: Any, threads: dict, caps: Any) -> list[dict]:
    """Every text of the bundle a quote could come from, labelled by origin (M3)."""
    from whygraph.analyze.rationale_generator import _collapse_whitespace, _json_list

    out: list[dict] = []

    def add(ref: str, origin: str, text: object) -> None:
        if isinstance(text, str) and text.strip():
            out.append(
                {"ref": ref, "origin": origin, "text": _collapse_whitespace(text)}
            )

    seen: set[str] = set()
    for item in evidence:
        c = item.commit
        ref = f"commit:{c.sha}"
        if ref not in seen:
            seen.add(ref)
            add(ref, "commit", c.subject)
            add(ref, "commit", c.body)
        for pr in item.pull_requests:
            ref = f"pr:{pr.number}"
            if ref in seen:
                continue
            seen.add(ref)
            add(ref, "pr_title", pr.title)
            add(ref, "pr_body", pr.body)
            for t in _json_list(pr.commit_titles)[: caps.pr_roster_max_commits]:
                if isinstance(t, dict):
                    add(ref, "pr_squashed_commit", t.get("headline"))
            for cm in _json_list(pr.comments)[: caps.pr_discussion_max_comments]:
                if isinstance(cm, dict):
                    body = (cm.get("body") or "").strip()[: caps.pr_comment_max_chars]
                    add(ref, "pr_conversation", body)
            block = threads.get(pr.number)
            if block is not None:
                for text in block.author_texts:
                    add(ref, "author_thread", text)
                for line in block.lines:
                    m = re.match(r"\s*-?\s*\[([^,\]]+), reviewer\] (.*)", line)
                    if m:
                        add(ref, "reviewer_thread", m.group(2))
        for issue in item.issues:
            ref = f"issue:{issue.number}"
            if ref not in seen:
                seen.add(ref)
                add(ref, "issue", issue.title)
                add(ref, "issue", issue.body)
    return out


def _target_code(root: Path, path: str, start: int, end: int) -> str:
    lines = (root / path).read_text(errors="replace").split("\n")
    return "\n".join(lines[start - 1 : end])


def cmd_generate(env: Env, args: argparse.Namespace) -> None:
    """Describe, then generate baseline x2 and treated x1 per corpus target."""
    from whygraph.analyze import RationaleGenerator
    from whygraph.analyze.rationale_generator import _PrRenderCaps, _select_threads
    from whygraph.core import get_config
    from whygraph.core.context import use_project
    from whygraph.core.usage import use_usage_sink
    from whygraph.db import get_session
    from whygraph.mcp.evidence import backfill_evidence_descriptions, review_scope
    from whygraph.mcp.rationale import _symbol_context
    from whygraph.mcp.targets import repo_root

    corpus = json.loads((EVAL_DIR / "part-a-corpus.json").read_text())
    ledger = Ledger(LEDGER, CAP_USD)
    price_in, price_out = _price_per_m()
    print(f"corpus {len(corpus)} targets; ledger ${ledger.total():.4f} of ${CAP_USD}")
    print(
        f"estimate: {len(corpus) * len(ARMS)} card calls on {PROVIDER}/{MODEL} "
        f"(${price_in}/${price_out} per M) at ~40k input / 2k output tokens = "
        f"${len(corpus) * len(ARMS) * (40e3 * price_in + 2e3 * price_out) / 1e6:.2f}, "
        f"plus up to {len(corpus) * 20} commit descriptions at ~8k / 0.4k = "
        f"${len(corpus) * 20 * (8e3 * price_in + 400 * price_out) / 1e6:.2f} "
        "(an upper bound: commits shared between targets are described once)"
    )
    if not args.yes:
        print("dry run: pass --yes to spend")
        return
    v2, cg = WORK / "v2" / "whygraph.db", WORK / "cg" / "codegraph.db"
    ctx = eval_context(env, v2, cg)
    portal_sink, writer = (None, None)
    if not args.no_portal_ledger:
        portal_sink, writer = _portal_sink(env, ctx.config, ctx.key_scopes)
    sink = EvalSink(ledger, ctx.config, portal_sink)
    CARDS.mkdir(parents=True, exist_ok=True)

    def allow() -> bool:
        try:
            ledger.check(0.02)
        except BudgetStop:
            return False
        return True

    try:
        with use_project(ctx), use_usage_sink(sink):
            caps = _PrRenderCaps.from_config(get_config().rationale)
            for row in corpus:
                tid = row["tid"]
                out = CARDS / f"{tid}.json"
                record = json.loads(out.read_text()) if out.exists() else {}
                if all(arm in record.get("cards", {}) for arm in ARMS):
                    continue
                print(f"{tid} {row['corpus_class']} {row['qualified_name']}")
                target = _target_of(Node(**{k: row[k] for k in Node.__annotations__}))
                from whygraph.mcp.evidence import collect_evidence

                evidence = collect_evidence(target, limit=20)
                _CURRENT_CALL.set((tid, "describe"))
                outcome = backfill_evidence_descriptions(
                    evidence,
                    target_path=target.path,
                    allow=allow,
                    max_commits=len(evidence),
                    wall_sec=900,
                )
                with get_session() as session:
                    scope = review_scope(session, repo_root(), target.path)
                rng = (target.line_start, target.line_end)
                selected = _select_threads(
                    evidence, scope.paths, scope.current_lines, rng, caps
                )
                symbol = _symbol_context(target)
                generator = RationaleGenerator.from_config(get_config())
                guarded = GuardedClient(generator._client, ledger, price_in, price_out)
                generator._client = guarded
                record.update(
                    {
                        "tid": tid,
                        "target": {
                            k: row[k]
                            for k in ("qualified_name", "path", "start", "end")
                        },
                        "corpus_class": row["corpus_class"],
                        "chosen_by": row["chosen_by"],
                        "evidence_commits": len(evidence),
                        "descriptions_waiting": sorted(outcome.waiting),
                        "rendered_threads": {
                            str(n): list(b.lines) for n, b in selected.items()
                        },
                        "author_thread_texts": {
                            str(n): list(b.author_texts) for n, b in selected.items()
                        },
                        "texts": _texts_by_origin(evidence, selected, caps),
                        "code": _target_code(env.root, target.path, *rng),
                    }
                )
                record.setdefault("cards", {})
                record.setdefault("prompts", {})
                for arm in ARMS:
                    if arm in record["cards"]:
                        continue
                    kwargs: dict[str, Any] = {}
                    if arm == "treated":
                        kwargs = {
                            "paths": scope.paths,
                            "file_lines": scope.current_lines,
                            "target_range": rng,
                        }
                    _CURRENT_CALL.set((tid, arm))
                    card = None
                    for attempt in range(2):
                        try:
                            card = generator.generate(
                                evidence,
                                symbol_context=symbol,
                                subject=target.qualified_name,
                                **kwargs,
                            )
                            break
                        except BudgetStop:
                            raise
                        except Exception as exc:  # noqa: BLE001 -- retried once, then recorded
                            print(f"  {arm} attempt {attempt + 1} failed: {exc}")
                            record.setdefault("errors", []).append(
                                {"arm": arm, "error": str(exc)}
                            )
                    record["prompts"][arm] = guarded.last_prompt
                    if card is not None:
                        record["cards"][arm] = _card_dict(card)
                        print(
                            f"  {arm}: {card.input_tokens} in / {card.output_tokens} "
                            f"out; ledger ${ledger.total():.4f}"
                        )
                    out.write_text(json.dumps(record, indent=1))
    except BudgetStop as exc:
        print(f"STOPPED before the cap: {exc}")
    finally:
        if writer is not None:
            writer.stop()
    print(f"ledger total ${ledger.total():.4f} of ${CAP_USD}")


# ---------------------------------------------------------------------------
# prompts (no LLM): pre-plan generator and v1 DB, for M1
# ---------------------------------------------------------------------------


def _old_generator() -> Any:
    """The pre-plan (``b466670``) generator module, loaded from git objects."""
    source = subprocess.run(
        ["git", "show", f"{PRE_PLAN_REV}:src/whygraph/analyze/rationale_generator.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    source = re.sub(r"^from \.(\w+)", r"from whygraph.analyze.\1", source, flags=re.M)
    source = re.sub(
        r"^from \. import", "from whygraph.analyze import", source, flags=re.M
    )
    path = WORK / "pre_plan_rationale_generator.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("pre_plan_generator", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


_SUMMARY_RE = re.compile(r"^  Summary: .*$", re.M)
_THREAD_ENTRY_RE = re.compile(r"^      - \[[^,\]\n]+, (?:PR author|reviewer)\] ", re.M)
"""The first entry of one rendered review thread (one per thread)."""


def _bundle_len(text: str) -> int:
    """Bundle characters without commit descriptions (v1 / v2 copies differ there)."""
    return len(_SUMMARY_RE.sub("", text))


def cmd_prompts(env: Env, args: argparse.Namespace) -> None:
    """Bundle sizes per target: v2 now, pre-plan generator on v2, v1 DB (no LLM)."""
    from whygraph.analyze.rationale_generator import _format_evidence, _PrRenderCaps
    from whygraph.core import get_config
    from whygraph.core.context import use_project
    from whygraph.mcp.evidence import collect_evidence

    corpus = json.loads((EVAL_DIR / "part-a-corpus.json").read_text())
    cg = WORK / "cg" / "codegraph.db"
    old = _old_generator()
    sizes: dict[str, dict] = {}
    for label, db in (
        ("v2", WORK / "v2" / "whygraph.db"),
        ("v1", WORK / "v1" / "whygraph.db"),
    ):
        if not db.exists():
            continue
        with use_project(eval_context(env, db, cg)):
            caps = _PrRenderCaps.from_config(get_config().rationale)
            old_caps = old._PrRenderCaps.from_config(get_config().rationale)
            for row in corpus:
                target = _target_of(Node(**{k: row[k] for k in Node.__annotations__}))
                evidence = collect_evidence(target, limit=20)
                entry = sizes.setdefault(row["tid"], {})
                entry[f"{label}_now"] = _bundle_len(
                    _format_evidence(evidence, caps, {})
                )
                entry[f"{label}_pre_plan"] = _bundle_len(
                    old._format_evidence(evidence, old_caps)
                )
                entry[f"{label}_commits"] = len(evidence)
    out = WORK / "bundle-sizes.json"
    out.write_text(json.dumps(sizes, indent=1))
    print(f"wrote {out}")


def cmd_m1(env: Env, args: argparse.Namespace) -> None:
    """M1 with no LLM: baseline and treated prompts per target, and the code checks.

    The prompts are built by the real generator with a client that answers
    an empty card, so they are exactly what a paid run would send, except
    that commit descriptions the lazy backfill has not written yet are
    missing in both arms alike. Writes ``part-a-work/m1.json``.
    """
    from whygraph.analyze import RationaleGenerator
    from whygraph.analyze.rationale_generator import _PrRenderCaps
    from whygraph.core import get_config
    from whygraph.core.context import use_project
    from whygraph.db import get_session
    from whygraph.mcp.evidence import collect_evidence, review_scope
    from whygraph.mcp.rationale import _symbol_context
    from whygraph.mcp.targets import repo_root

    corpus = json.loads((EVAL_DIR / "part-a-corpus.json").read_text())
    v2, cg = WORK / "v2" / "whygraph.db", WORK / "cg" / "codegraph.db"
    rows = []
    with use_project(eval_context(env, v2, cg)):
        client = PromptOnlyClient()
        generator = RationaleGenerator(
            client, caps=_PrRenderCaps.from_config(get_config().rationale)
        )
        for row in corpus:
            target = _target_of(Node(**{k: row[k] for k in Node.__annotations__}))
            evidence = collect_evidence(target, limit=20)
            with get_session() as session:
                scope = review_scope(session, repo_root(), target.path)
            symbol = _symbol_context(target)
            prompts = {}
            for arm, kwargs in (
                ("baseline1", {}),
                (
                    "treated",
                    {
                        "paths": scope.paths,
                        "file_lines": scope.current_lines,
                        "target_range": (target.line_start, target.line_end),
                    },
                ),
            ):
                generator.generate(
                    evidence,
                    symbol_context=symbol,
                    subject=row["qualified_name"],
                    **kwargs,
                )
                prompts[arm] = client.last_prompt
            record = {
                "tid": row["tid"],
                "corpus_class": row["corpus_class"],
                "prompts": prompts,
            }
            fails = _code_checks(record)
            if row["corpus_class"] == "thread":
                fails += [
                    f"{k} on other code rendered"
                    for k in _rendered_other_threads(record, row, v2)
                ]
            other = [
                t["key"]
                for t in row["threads"]
                if t["rules_1_2"] and t["positions"] and not t["in_range"]
            ]
            b, t = prompts["baseline1"]["user"], prompts["treated"]["user"]
            rows.append(
                {
                    "tid": row["tid"],
                    "class": row["corpus_class"],
                    "baseline_chars": len(b),
                    "treated_chars": len(t),
                    "delta": len(t) - len(b),
                    "thread_blocks": t.count("Review threads on these lines"),
                    "threads_rendered": len(_THREAD_ENTRY_RE.findall(t)),
                    "other_code_threads_checked": other,
                    "failures": fails,
                }
            )
            print(
                f"  {row['tid']} {row['corpus_class']:<8} {len(t) - len(b):+6d} {fails}"
            )
    out = WORK / "m1.json"
    out.write_text(json.dumps(rows, indent=1))
    print(f"wrote {out}")


# ---------------------------------------------------------------------------
# measure
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z`(\"'])", text.strip())
    return [p.strip() for p in parts if len(p.strip()) > 15]


def _units(card: dict) -> list[dict]:
    """A card's judgeable units: items (constraints, risks, tradeoffs) and why sentences."""
    units: list[dict] = []
    for kind in ("constraints", "risks"):
        for item in card[kind]:
            units.append({"kind": kind[:-1], **item})
    for t in card["tradeoffs"]:
        units.append(
            {"kind": "tradeoff", "text": t, "basis": "-", "refs": [], "quote": ""}
        )
    for s in _sentences(card["why"]):
        units.append({"kind": "why", "text": s, "basis": "-", "refs": [], "quote": ""})
    return units


def _card_chars(card: dict) -> int:
    return (
        len(card["purpose"])
        + len(card["why"])
        + sum(len(i["text"]) for i in card["constraints"] + card["risks"])
        + sum(len(t) for t in card["tradeoffs"])
    )


def _n_items(card: dict) -> int:
    return len(card["constraints"]) + len(card["risks"]) + len(card["tradeoffs"])


def _present(unit: dict, others: list[dict]) -> bool:
    """Whether a similar unit (same claim, fuzzy) is in ``others``."""
    for o in others:
        if (
            unit["quote"]
            and o.get("quote")
            and (unit["quote"] in o["quote"] or o["quote"] in unit["quote"])
        ):
            return True
        if _similar(unit["text"], o["text"]) >= FUZZY_SAME:
            return True
    return False


def _quote_origins(item: dict, texts: list[dict]) -> list[str]:
    """Where an item's quote is found among its refs' texts (M3)."""
    if not item.get("quote"):
        return []
    refs = {f"{r['kind']}:{r['id']}" for r in item["refs"]}
    needle = _norm(item["quote"])
    found = []
    for t in texts:
        if refs and not any(t["ref"] == r or t["ref"].startswith(r) for r in refs):
            continue
        if needle and needle in _norm(t["text"]):
            found.append(t["origin"])
    return sorted(set(found))


def _any_origin(item: dict, texts: list[dict]) -> list[str]:
    """Where a cited item's quote text appears anywhere in the bundle (any ref)."""
    needle = _norm(item.get("quote") or "")
    if len(needle) < 12:
        return []
    return sorted({t["origin"] for t in texts if needle in _norm(t["text"])})


def _pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    k = max(0, min(len(values) - 1, round(q * (len(values) - 1))))
    return values[k]


def _code_checks(record: dict) -> list[str]:
    """M1's code checks; an empty list means every check holds."""
    failures = []
    prompts = record["prompts"]
    cls = record["corpus_class"]
    header = "Review threads on these lines"
    treated = (prompts.get("treated") or {}).get("user", "")
    base = (prompts.get("baseline1") or {}).get("user", "")
    if header in base:
        failures.append("a baseline prompt renders review threads")
    if cls in ("filtered", "stale", "control"):
        if header in treated:
            failures.append(f"{cls}: the treated prompt renders review threads")
        if treated != base:
            failures.append(f"{cls}: treated prompt differs from baseline")
    if cls == "thread" and header not in treated:
        failures.append("thread target: no review-thread block in the treated prompt")
    return failures


def _rendered_other_threads(record: dict, survey_row: dict, v2: Path) -> list[str]:
    """Openers of same-file threads on other code that appear in the treated prompt."""
    from whygraph.analyze.rationale_generator import _clean_thread_comment

    treated = (record["prompts"].get("treated") or {}).get("user", "")
    prs = {n: ts for n, _, _, ts in _pr_rows(v2)}
    leaks = []
    for t in survey_row["threads"]:
        if not (t["rules_1_2"] and t["positions"] and not t["in_range"]):
            continue
        idx = int(t["key"].split("/t")[1])
        thread = prs[t["pr"]][idx]
        for c in thread.get("comments") or []:
            text = _clean_thread_comment(c.get("body"))[:400]
            if len(text) >= 40 and text in treated:
                leaks.append(t["key"])
                break
    return leaks


def cmd_measure(env: Env, args: argparse.Namespace) -> None:
    """Compute M1, M2, M3, M5, Gain; write the report and the M4 / M6 sheets."""
    corpus = json.loads((EVAL_DIR / "part-a-corpus.json").read_text())
    survey = {r["id"]: r for r in json.loads((WORK / "survey.json").read_text())}
    sizes_path = WORK / "bundle-sizes.json"
    sizes = json.loads(sizes_path.read_text()) if sizes_path.exists() else {}
    v2 = WORK / "v2" / "whygraph.db"
    records = []
    for row in corpus:
        path = CARDS / f"{row['tid']}.json"
        if path.exists():
            rec = json.loads(path.read_text())
            if all(a in rec.get("cards", {}) for a in ARMS):
                rec["survey"] = survey.get(row["id"])
                records.append(rec)
    missing = len(corpus) - len(records)
    thread_recs = [r for r in records if r["corpus_class"] == "thread"]

    # M1 prompt size.
    m1_rows = []
    m1_fail: dict[str, list[str]] = {}
    for rec in records:
        p = rec["prompts"]
        t_len = len(p["treated"]["user"])
        b_len = len(p["baseline1"]["user"])
        m1_rows.append((rec, t_len - b_len, b_len))
        fails = _code_checks(rec)
        if rec["corpus_class"] == "thread" and rec["survey"]:
            leaks = _rendered_other_threads(rec, rec["survey"], v2)
            fails += [f"{k} on other code rendered" for k in leaks]
        if fails:
            m1_fail[rec["tid"]] = fails
    thread_deltas = [d for rec, d, _ in m1_rows if rec["corpus_class"] == "thread"]

    # M2 card size.
    m2 = []
    for rec in thread_recs:
        c = rec["cards"]
        b1, b2, t = c["baseline1"], c["baseline2"], c["treated"]
        bmean_items = (_n_items(b1) + _n_items(b2)) / 2
        bmean_chars = (_card_chars(b1) + _card_chars(b2)) / 2
        m2.append(
            {
                "tid": rec["tid"],
                "items": (_n_items(b1), _n_items(b2), _n_items(t)),
                "items_delta": _n_items(t) - bmean_items,
                "items_noise": abs(_n_items(b2) - _n_items(b1)),
                "chars": (_card_chars(b1), _card_chars(b2), _card_chars(t)),
                "chars_delta_pct": (_card_chars(t) - bmean_chars) / bmean_chars * 100,
                "chars_noise_pct": abs(_card_chars(b2) - _card_chars(b1))
                / bmean_chars
                * 100,
            }
        )
    med_items = (
        statistics.median([x["items_delta"] for x in m2]) if m2 else float("nan")
    )
    med_items_noise = (
        statistics.median([x["items_noise"] for x in m2]) if m2 else float("nan")
    )
    med_chars = (
        statistics.median([x["chars_delta_pct"] for x in m2]) if m2 else float("nan")
    )
    med_chars_noise = (
        statistics.median([x["chars_noise_pct"] for x in m2]) if m2 else float("nan")
    )
    m2_items_pass = med_items <= 1
    m2_chars_pass = med_chars - med_chars_noise <= 15

    # M3 quote origin, M5 displacement, Gain.
    m3_counts: dict[str, Counter] = {arm: Counter() for arm in ARMS}
    m3_items: dict[str, int] = {arm: 0 for arm in ARMS}
    only_thread = 0
    gain_targets: dict[str, list[str]] = {}
    displaced_t = displaced_b = 0
    m5_rows = []
    for rec in records:
        texts = rec["texts"]
        for arm in ARMS:
            for item in rec["cards"][arm]["constraints"] + rec["cards"][arm]["risks"]:
                m3_items[arm] += 1
                if item["basis"] == "quoted":
                    origins = _quote_origins(item, texts)
                    for o in origins or ["(not found)"]:
                        m3_counts[arm][o] += 1
                    if arm == "treated" and origins == ["author_thread"]:
                        only_thread += 1
                elif item["basis"] == "cited":
                    m3_counts[arm]["cited"] += 1
                    anywhere = _any_origin(item, texts)
                    if "reviewer_thread" in anywhere:
                        m3_counts[arm]["cited (quote is reviewer thread text)"] += 1
                else:
                    m3_counts[arm][item["basis"]] += 1
        if rec["corpus_class"] == "thread":
            hits = []
            for item in (
                rec["cards"]["treated"]["constraints"]
                + rec["cards"]["treated"]["risks"]
            ):
                origins = _quote_origins(item, texts)
                anywhere = _any_origin(item, texts)
                if item["basis"] == "quoted" and "author_thread" in origins:
                    hits.append(f"quoted: {item['text'][:80]}")
                elif item["basis"] == "cited" and "author_thread" in anywhere:
                    hits.append(f"cited: {item['text'][:80]}")
            if hits:
                gain_targets[rec["tid"]] = hits
        c = rec["cards"]
        q1 = [
            i
            for i in c["baseline1"]["constraints"] + c["baseline1"]["risks"]
            if i["basis"] == "quoted"
        ]
        b2_units = c["baseline2"]["constraints"] + c["baseline2"]["risks"]
        t_units = c["treated"]["constraints"] + c["treated"]["risks"]
        lost_t = sum(1 for i in q1 if not _present(i, t_units))
        lost_b = sum(1 for i in q1 if not _present(i, b2_units))
        displaced_t += lost_t
        displaced_b += lost_b
        m5_rows.append((rec["tid"], len(q1), lost_b, lost_t))
    displacement = displaced_t - displaced_b
    m5_pass = displacement <= 1
    gain_pass = len(gain_targets) >= 5

    # M4 sheet: treated units absent from both baselines.
    m4_lines = [
        "# Plan 62 Part A - M4 precision sheet (owner, hand-judged)",
        "",
        "Every item or `why` sentence of a **treated** card that is absent from **both** "
        f"baseline cards (fuzzy match, `SequenceMatcher` ratio >= {FUZZY_SAME}, or a "
        "shared quote). Thread targets only. For each, read the target code and the "
        "evidence, then fill the three verdict columns with `y` / `n` / `?`.",
        "",
        "- **True?** Is the claim true of today's code (the code block shown)?",
        "- **Rejected / open?** Is it a rejected suggestion or an open question "
        "presented as fact (`y` = yes, it is one; this must be `n`)?",
        "- **On target?** Is it about the target symbol (not other code in the file)?",
        "",
        "Bar (plan section 9): 0 rejected-suggestion constraints; at least 90 % precise "
        "(precise = True `y`, Rejected `n`, On target `y`).",
        "",
    ]
    m4_count = 0
    for rec in thread_recs:
        c = rec["cards"]
        base_units = _units(c["baseline1"]) + _units(c["baseline2"])
        new = [u for u in _units(c["treated"]) if not _present(u, base_units)]
        if not new:
            continue
        tgt = rec["target"]
        m4_lines += [
            f"## {rec['tid']} - `{tgt['qualified_name']}`",
            "",
            f"`{tgt['path']}` lines {tgt['start']}-{tgt['end']}",
            "",
            "<details><summary>Target code (today)</summary>",
            "",
            "```",
            rec["code"][:6000],
            "```",
            "",
            "</details>",
            "",
            "<details><summary>Review threads the treated prompt rendered</summary>",
            "",
            "```",
            *[line for lines in rec["rendered_threads"].values() for line in lines],
            "```",
            "",
            "</details>",
            "",
        ]
        for u in new:
            m4_count += 1
            origins = _quote_origins(u, rec["texts"]) if u["quote"] else []
            refs = ", ".join(f"{r['kind']} {r['id'][:10]}" for r in u["refs"]) or "-"
            m4_lines += [
                f"### M4-{m4_count:03d} ({u['kind']}, basis `{u['basis']}`)",
                "",
                f"> {u['text']}",
                "",
                f"- Refs: {refs}",
                f"- Quote: {u['quote'] or '-'}"
                + (f" (found in: {', '.join(origins)})" if origins else ""),
                "",
                "| True of today's code? | Rejected suggestion / open question? | About the target symbol? | Note |",
                "|---|---|---|---|",
                "|   |   |   |   |",
                "",
            ]
    m4_lines.insert(4, f"Units to judge: **{m4_count}**.")
    m4_lines.insert(5, "")
    (EVAL_DIR / "part-a-m4-sheet.md").write_text("\n".join(m4_lines) + "\n")

    # M6 sheet: blind pairwise, thread targets.
    rnd = random.Random(args.seed)
    key: dict[str, dict] = {}
    m6 = [
        "# Plan 62 Part A - M6 blind focus comparison (owner)",
        "",
        "For each target, two cards of the same code in random order. Which card states "
        "the target's reason more accurately and with less irrelevant content? Write "
        "`A`, `B` or `=` in the verdict line. The answer key is in "
        "`part-a-m6-key.json`: do not open it until every verdict is in.",
        "",
        "Bar (plan section 9): the treated card is worse on at most 2 of 15.",
        "",
    ]

    def render(card: dict) -> list[str]:
        out = [f"**Purpose.** {card['purpose']}", "", f"**Why.** {card['why']}", ""]
        for kind in ("constraints", "risks"):
            if card[kind]:
                out.append(f"**{kind.capitalize()}.**")
                for i in card[kind]:
                    refs = ", ".join(f"{r['kind']} {r['id'][:10]}" for r in i["refs"])
                    out.append(f"- {i['text']}" + (f" ({refs})" if refs else ""))
                out.append("")
        if card["tradeoffs"]:
            out.append("**Tradeoffs.**")
            out += [f"- {t}" for t in card["tradeoffs"]]
            out.append("")
        return out

    for n, rec in enumerate(thread_recs, 1):
        pair = ["baseline1", "treated"]
        rnd.shuffle(pair)
        label = f"M6-{n:02d}"
        key[label] = {"tid": rec["tid"], "A": pair[0], "B": pair[1]}
        tgt = rec["target"]
        m6 += [
            f"## {label} - `{tgt['qualified_name']}`",
            "",
            f"`{tgt['path']}` lines {tgt['start']}-{tgt['end']}",
            "",
            "<details><summary>Target code (today)</summary>",
            "",
            "```",
            rec["code"][:6000],
            "```",
            "",
            "</details>",
            "",
            "### Card A",
            "",
            *render(rec["cards"][pair[0]]),
            "### Card B",
            "",
            *render(rec["cards"][pair[1]]),
            "**Verdict (A / B / =):** ",
            "",
            "---",
            "",
        ]
    (EVAL_DIR / "part-a-m6-sheet.md").write_text("\n".join(m6) + "\n")
    (EVAL_DIR / "part-a-m6-key.json").write_text(json.dumps(key, indent=1) + "\n")

    # Report.
    ledger_total = Ledger(LEDGER, CAP_USD).total()
    spend = Counter()
    for line in LEDGER.read_text().splitlines():
        if line.strip():
            e = json.loads(line)
            spend[e.get("arm", "?")] += float(e.get("cost") or 0)

    def yn(ok: bool) -> str:
        return "**PASS**" if ok else "**FAIL**"

    rep = [
        "# Plan 62 Part A - eval report (section 9)",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} by "
        "`scripts/rationale_thread_eval.py measure`. Model "
        f"`{PROVIDER}/{MODEL}` (the cheapest the dev setup has; no `deepseek-flash` key). "
        f"Targets with all three arms: {len(records)} of {len(corpus)}"
        + (f" ({missing} missing)" if missing else "")
        + ".",
        "",
        "## Spend",
        "",
        f"Ledger `plans/62-eval/spend.jsonl`: **${ledger_total:.4f} of ${CAP_USD:.2f}**. "
        "By arm: " + ", ".join(f"{k} ${v:.4f}" for k, v in sorted(spend.items())) + ".",
        "",
        "## Corpus",
        "",
        "| Target | Class | Symbol | Lines | Commits | Chosen by |",
        "|---|---|---|---|---|---|",
    ]
    for rec in records:
        t = rec["target"]
        rep.append(
            f"| {rec['tid']} | {rec['corpus_class']} | `{t['qualified_name']}` "
            f"(`{t['path']}`) | {t['start']}-{t['end']} | {rec['evidence_commits']} | "
            f"{rec['chosen_by']} |"
        )
    rep += [
        "",
        "## M1 - prompt size",
        "",
        "Treated minus baseline prompt characters (the user message), both arms on the "
        "same v2 database. De-duplication of a PR's discussion is in both arms (the "
        "baseline is today's generator with `paths=None`), so the delta is the "
        "review-thread blocks only; the pre-plan columns below measure de-duplication.",
        "",
        f"- Thread targets ({len(thread_deltas)}): median "
        f"{statistics.median(thread_deltas) if thread_deltas else float('nan'):+.0f}, "
        f"p90 {_pct(thread_deltas, 0.9):+.0f}, max "
        f"{max(thread_deltas) if thread_deltas else float('nan'):+.0f} characters.",
        "- Code checks (no thread block in any baseline; none in the treated prompt of a "
        "filtered, stale or control target and the treated prompt byte-identical to the "
        "baseline there; a block in every thread target's treated prompt; no thread on "
        "other code of the same file rendered (R21)): "
        + ("**all hold**" if not m1_fail else f"**{len(m1_fail)} failing**"),
    ]
    for tid, fails in m1_fail.items():
        rep.append(f"  - {tid}: {'; '.join(fails)}")
    rep += [
        "",
        "| Target | Class | Baseline chars | Treated - baseline |",
        "|---|---|---|---|",
    ]
    for rec, d, b in m1_rows:
        rep.append(f"| {rec['tid']} | {rec['corpus_class']} | {b} | {d:+d} |")
    if sizes:
        rep += [
            "",
            "Bundle text only, commit descriptions removed (no LLM): v2 database with "
            "today's generator, v2 with the pre-plan generator (`b466670`, no "
            "de-duplication), and the v1 database (before R19's bot removal) with "
            "today's generator.",
            "",
            "| Target | v2 now | v2 pre-plan | de-dup saves | v1 now | bot removal saves |",
            "|---|---|---|---|---|---|",
        ]
        dedup, bots = [], []
        for rec in records:
            s = sizes.get(rec["tid"], {})
            if "v2_now" not in s:
                continue
            dd = s["v2_pre_plan"] - s["v2_now"]
            dedup.append(dd)
            v1 = s.get("v1_now")
            bb = (v1 - s["v2_now"]) if v1 is not None else None
            if bb is not None:
                bots.append(bb)
            rep.append(
                f"| {rec['tid']} | {s['v2_now']} | {s['v2_pre_plan']} | {dd} | "
                f"{v1 if v1 is not None else '-'} | {bb if bb is not None else '-'} |"
            )
        if dedup:
            rep.append(
                f"\nDe-duplication saves median {statistics.median(dedup):.0f}, max "
                f"{max(dedup)} characters per bundle."
            )
        if bots:
            rep.append(
                f"v1 minus v2 (bots and anything else the re-crawl changed): median "
                f"{statistics.median(bots):.0f}, p90 {_pct(bots, 0.9):.0f}, max "
                f"{max(bots)} characters per bundle. The v1 copy predates this scan, "
                "so commit and PR sets can differ slightly."
            )
    rep += [
        "",
        "## M2 - card size (thread targets)",
        "",
        "Items = constraints + risks + tradeoffs; characters = purpose + why + item texts. "
        "Delta = treated minus the mean of the two baselines; noise = |baseline2 - "
        "baseline1|.",
        "",
        f"- Items: median delta {med_items:+.1f} (noise median {med_items_noise:.1f}); "
        f"bar +1 or less: {yn(m2_items_pass)}",
        f"- Characters: median delta {med_chars:+.1f} % (noise median "
        f"{med_chars_noise:.1f} %); bar +15 % or less over the noise floor "
        f"(delta - noise = {med_chars - med_chars_noise:+.1f} %): {yn(m2_chars_pass)}",
        "",
        "| Target | Items b1 / b2 / t | Chars b1 / b2 / t | Chars delta % |",
        "|---|---|---|---|",
    ]
    for x in m2:
        rep.append(
            f"| {x['tid']} | {' / '.join(map(str, x['items']))} | "
            f"{' / '.join(map(str, x['chars']))} | {x['chars_delta_pct']:+.1f} |"
        )
    rep += [
        "",
        "## M3 - quote origin (constraints and risks, all targets)",
        "",
        "Where each item's quote is found among its refs' texts (an item can be found in "
        "more than one); `cited` / `inferred` items hold no verified quote.",
        "",
        "| Origin | " + " | ".join(ARMS) + " |",
        "|---|" + "---|" * len(ARMS),
    ]
    origins = sorted({o for c in m3_counts.values() for o in c})
    for o in origins:
        rep.append(f"| {o} | " + " | ".join(str(m3_counts[a][o]) for a in ARMS) + " |")
    rep.append("| (items) | " + " | ".join(str(m3_items[a]) for a in ARMS) + " |")
    share = only_thread / m3_items["treated"] * 100 if m3_items["treated"] else 0
    rep += [
        "",
        f"Treated items supported only by a PR-author thread comment: {only_thread} of "
        f"{m3_items['treated']} ({share:.1f} %).",
        "",
        "## M5 - displacement",
        "",
        "Baseline-1 `quoted` items with no match (fuzzy text or shared quote) in the "
        "treated card, minus those with no match in baseline 2.",
        "",
        f"- Lost in treated: {displaced_t}; lost baseline-to-baseline: {displaced_b}; "
        f"displacement {displacement:+d}. Bar at most 1: {yn(m5_pass)}",
        "",
        "| Target | b1 quoted | lost in b2 | lost in treated |",
        "|---|---|---|---|",
    ]
    rep += [f"| {t} | {q} | {lb} | {lt} |" for t, q, lb, lt in m5_rows]
    rep += [
        "",
        "## Gain",
        "",
        "Thread targets whose treated card has a `quoted` item whose quote is in a "
        "PR-author thread comment, or a `cited` item whose quote text appears in one.",
        "",
        f"- {len(gain_targets)} of {len(thread_recs)} thread targets; bar at least 5 of "
        f"15: {yn(gain_pass)}",
    ]
    for tid, hits in gain_targets.items():
        rep.append(f"  - {tid}: " + "; ".join(hits))
    rep += [
        "",
        "## M4 - precision: awaiting owner",
        "",
        f"`plans/62-eval/part-a-m4-sheet.md`: {m4_count} treated units absent from both "
        "baselines. Bar: 0 rejected-suggestion constraints; at least 90 % precise.",
        "",
        "## M6 - focus: awaiting owner",
        "",
        f"`plans/62-eval/part-a-m6-sheet.md`: {len(thread_recs)} blind A/B pairs "
        "(baseline 1 vs treated); answer key `plans/62-eval/part-a-m6-key.json`. Bar: "
        "treated worse on at most 2 of 15.",
        "",
        "Raw cards, prompts and evidence texts: `plans/62-eval/part-a-cards/<target>.json`.",
    ]
    (EVAL_DIR / "part-a-report.md").write_text("\n".join(rep) + "\n")
    print(
        "wrote part-a-report.md, part-a-m4-sheet.md, part-a-m6-sheet.md, part-a-m6-key.json"
    )
    print(
        json.dumps(
            {
                "m2_items": [med_items, m2_items_pass],
                "m2_chars": [med_chars, med_chars_noise, m2_chars_pass],
                "m5": [displaced_t, displaced_b, m5_pass],
                "gain": [len(gain_targets), gain_pass],
                "m1_failures": m1_fail,
                "m4_units": m4_count,
                "spend": ledger_total,
            },
            indent=1,
        )
    )


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run one sub-command.

    Parameters
    ----------
    argv : list of str, optional
        The arguments (default: ``sys.argv[1:]``).

    Returns
    -------
    int
        The exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--slug", default="biome")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument(
        "--db-url", default=os.environ.get("WHYGRAPH_DATABASE_URL", DEFAULT_DB_URL)
    )
    parser.add_argument("--seed", type=int, default=62)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("copy")
    p.add_argument("--v1-db", help="a WhyGraph DB from before the v2 crawl (M1)")
    p = sub.add_parser("survey")
    p.add_argument("--live", type=int, default=80)
    p.add_argument("--unqualified", type=int, default=20)
    p.add_argument("--gone", type=int, default=25)
    p.add_argument("--dup", type=int, default=15)
    p.add_argument("--control-files", type=int, default=12)
    p = sub.add_parser("candidates")
    p.add_argument("--min-comments", type=int, default=3)
    p.add_argument("--chars", type=int, default=300)
    p = sub.add_parser("select")
    p.add_argument("--adversarial", help="JSON list of {id, reason}")
    p = sub.add_parser("generate")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--no-portal-ledger", action="store_true")
    sub.add_parser("prompts")
    sub.add_parser("m1")
    sub.add_parser("measure")
    args = parser.parse_args(argv)
    env = Env(slug=args.slug, data=args.data.expanduser(), db_url=args.db_url)
    commands = {
        "copy": cmd_copy,
        "survey": cmd_survey,
        "candidates": cmd_candidates,
        "select": cmd_select,
        "generate": cmd_generate,
        "prompts": cmd_prompts,
        "m1": cmd_m1,
        "measure": cmd_measure,
    }
    commands[args.cmd](env, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
