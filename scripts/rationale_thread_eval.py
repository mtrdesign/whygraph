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

Part C's grounding arms (plan 62 section 14.8, R48; step C7) reuse the same
copies and the 15 thread targets, at ``medium`` with today's bundle::

    uv run --no-sync python scripts/rationale_thread_eval.py \\
        --model claude-sonnet-5-5 grounding            # prompts + estimate only
    ... grounding --yes                                # G0 x2, G1, G2 (60 cards)
    ... grounding-sheets                               # blind sheets + keys
    ... grounding-summary                              # sizes, spend, verdicts

The level arms (section 14.8 (ii), after R49: ``low`` against ``medium``)
use G2's prompt; ``medium`` reuses the G2 cards when ``level-prompts`` finds
today's prompt unchanged::

    ... level-prompts                                  # sizes, reuse check, estimate
    ... grounding --level low --cap 25 --yes           # arms g2 and g2b (noise)
    ... level-sheets                                   # blind claims + pairs, keys apart

The verification pass that ``grounding-verify`` ran is gone from the product
(R49); its results stay in the stored cards (``verify``).

G0 is today's generator with the pre-C3a ``system.md`` and labels injected
here (from ``1ae370e^``), never in ``src/``.

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
STEP = "plan62-part-a"
"""The ``step`` of every ledger line this run writes."""

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
    reserve : float, optional
        Kept free below the cap; default :data:`RESERVE_USD`.
    """

    def __init__(self, path: Path, cap: float, reserve: float = RESERVE_USD) -> None:
        self.path = path
        self.cap = cap
        self.reserve = reserve
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
            When the total plus ``upcoming`` plus the reserve exceeds the cap.
        """
        spent = self.total()
        if spent + upcoming + self.reserve > self.cap:
            raise BudgetStop(
                f"ledger at ${spent:.4f}; next call ~${upcoming:.4f} would cross "
                f"${self.cap:.2f} (reserve ${self.reserve:.2f})"
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


def eval_context(
    env: Env,
    whygraph_db: Path,
    codegraph_db: Path,
    *,
    root: Path | None = None,
    slug: str | None = None,
) -> Any:
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
    root : Path, optional
        Another repository's checkout (an external corpus, read-only); the
        keys and config still come from ``env``'s project.
    slug : str, optional
        The context's slug (default ``env.slug``).

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
    root = root if root is not None else env.root
    assert root is not None
    config = Config.from_dict(merged, root)
    for task in ("analyze", "rationale"):
        choice = tuple(config.model_for(task))
        if choice != (PROVIDER, MODEL):
            raise SystemExit(f"{task} resolves to {choice}, not {PROVIDER}/{MODEL}")
    if key_scopes.get(PROVIDER, "none") == "none":
        raise SystemExit(f"the portal stores no {PROVIDER} key for {env.slug!r}")
    return ProjectContext(
        slug=slug or env.slug, root=root, config=config, key_scopes=key_scopes
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
                "step": STEP,
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
    chars_per_token : float
        Prompt characters per input token assumed by the estimate.
    """

    def __init__(
        self,
        inner: Any,
        ledger: Ledger,
        price_in: float,
        price_out: float,
        max_out_tokens: int = 4000,
        chars_per_token: float = 3.0,
    ) -> None:
        self._inner = inner
        self._ledger = ledger
        self._price_in = price_in
        self._price_out = price_out
        self._max_out = max_out_tokens
        self._cpt = chars_per_token
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
            chars / self._cpt * self._price_in + self._max_out * self._price_out
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
    if args.classes:
        wanted = set(args.classes.split(","))
        corpus = [r for r in corpus if r["corpus_class"] in wanted]
    arms = tuple(a.strip() for a in args.arms.split(",") if a.strip())
    ledger = Ledger(LEDGER, CAP_USD)
    price_in, price_out = _price_per_m()
    print(f"corpus {len(corpus)} targets; ledger ${ledger.total():.4f} of ${CAP_USD}")
    print(
        f"estimate: {len(corpus) * len(arms)} card calls on {PROVIDER}/{MODEL} "
        f"(${price_in}/${price_out} per M) at ~40k input / 2k output tokens = "
        f"${len(corpus) * len(arms) * (40e3 * price_in + 2e3 * price_out) / 1e6:.2f}, "
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
        if args.no_describe:
            return False  # cached descriptions still apply; no new describe call
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
                if all(arm in record.get("cards", {}) for arm in arms):
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
                for arm in arms:
                    if arm in record["cards"]:
                        continue
                    kwargs: dict[str, Any] = {}
                    if "treated" in arm:
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


_STOP = frozenset(
    "the a an of to and or in is are be for on with that this it by as its not "
    "from at when which so if than can".split()
)


def _content_words(text: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z0-9_`:]+", text.lower())
        if len(w) > 2 and w not in _STOP
    }


def _thread_link(unit: dict, rec: dict) -> str:
    """Why a card unit is linked to a rendered thread, or ``""`` (M4 triage only)."""
    threaded = set(rec["rendered_threads"])
    prs = [r["id"] for r in unit["refs"] if r["kind"] == "pr" and r["id"] in threaded]
    if prs:
        return "cites PR #" + ", #".join(prs) + ", whose thread was rendered"
    words = _content_words(unit["text"])
    thread_words = _content_words(
        " ".join(line for lines in rec["rendered_threads"].values() for line in lines)
    )
    if words and len(words & thread_words) / len(words) >= 0.4:
        return "shares content words with the rendered thread"
    return ""


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
    m5_lost: list[tuple[str, dict, bool, list[str]]] = []
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
        lost_t_items = [i for i in q1 if not _present(i, t_units)]
        lost_b = sum(1 for i in q1 if not _present(i, b2_units))
        lost_t = len(lost_t_items)
        displaced_t += lost_t
        displaced_b += lost_b
        m5_rows.append((rec["tid"], len(q1), lost_b, lost_t))
        for i in lost_t_items:
            m5_lost.append(
                (rec["tid"], i, _present(i, b2_units), _quote_origins(i, rec["texts"]))
            )
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
    m4_linked = 0
    noise_total = noise_new = 0
    for rec in thread_recs:
        c = rec["cards"]
        b2 = _units(c["baseline2"])
        noise_total += len(b2)
        noise_new += sum(1 for u in b2 if not _present(u, _units(c["baseline1"])))
    m4_lines += [
        f"Noise floor: {noise_new} of {noise_total} baseline-2 units are absent from "
        "baseline 1 under the same matcher, so most units below differ from the "
        "baselines by run-to-run variance, not by the threads. Units marked "
        "**thread-linked** (they cite a PR whose thread was rendered, or share at "
        "least 40 % of their content words with the rendered thread text) are listed "
        "first in each target: the rejected-suggestion risk is concentrated there.",
        "",
    ]
    for rec in thread_recs:
        c = rec["cards"]
        base_units = _units(c["baseline1"]) + _units(c["baseline2"])
        new = [u for u in _units(c["treated"]) if not _present(u, base_units)]
        if not new:
            continue
        for u in new:
            u["thread_linked"] = _thread_link(u, rec)
        new.sort(key=lambda u: not u["thread_linked"])
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
            m4_linked += bool(u["thread_linked"])
            origins = _quote_origins(u, rec["texts"]) if u["quote"] else []
            refs = ", ".join(f"{r['kind']} {r['id'][:10]}" for r in u["refs"]) or "-"
            link = (
                f" - **thread-linked** ({u['thread_linked']})"
                if u["thread_linked"]
                else ""
            )
            m4_lines += [
                f"### M4-{m4_count:03d} ({u['kind']}, basis `{u['basis']}`){link}",
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
    m4_lines.insert(4, f"Units to judge: **{m4_count}** ({m4_linked} thread-linked).")
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
    if m5_lost:
        rep += ["", "Baseline-1 quoted items missing from the treated card:", ""]
        for tid, item, in_b2, origins in m5_lost:
            rep.append(
                f"- {tid} ({'also in' if in_b2 else 'also missing from'} baseline 2; quote "
                f"from {', '.join(origins) or '?'}): {item['text']}"
            )
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
        f"baselines ({m4_linked} thread-linked, listed first per target). Noise floor: "
        f"{noise_new} of {noise_total} baseline-2 units are absent from baseline 1 under "
        "the same matcher, so most of the sheet is run-to-run variance. Bar: 0 "
        "rejected-suggestion constraints; at least 90 % precise.",
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
# repeats: M5, M2 and Gain over every baseline and treated pass
# ---------------------------------------------------------------------------

REPEATS_START = "<!-- repeats:start -->"
REPEATS_END = "<!-- repeats:end -->"
BOOTSTRAP = 5000


def _quoted(card: dict) -> list[dict]:
    return [i for i in card["constraints"] + card["risks"] if i["basis"] == "quoted"]


def _losses(src: dict, dst: dict) -> tuple[int, int]:
    """``(quoted items of src, how many have no match in dst)``."""
    q = _quoted(src)
    dst_items = dst["constraints"] + dst["risks"]
    return len(q), sum(1 for i in q if not _present(i, dst_items))


def _gain_hit(card: dict, texts: list[dict]) -> bool:
    for item in card["constraints"] + card["risks"]:
        if item["basis"] == "quoted" and "author_thread" in _quote_origins(item, texts):
            return True
        if item["basis"] == "cited" and "author_thread" in _any_origin(item, texts):
            return True
    return False


def _mean_abs_pair(values: list[float]) -> float:
    pairs = [abs(a - b) for i, a in enumerate(values) for b in values[i + 1 :]]
    return statistics.mean(pairs) if pairs else 0.0


def cmd_repeats(env: Env, args: argparse.Namespace) -> None:
    """M5, M2 and Gain over all passes; writes a section into ``part-a-report.md``."""
    recs = []
    for path in sorted(CARDS.glob("T*.json")):
        rec = json.loads(path.read_text())
        bases = sorted(a for a in rec["cards"] if a.startswith("baseline"))
        treats = sorted(a for a in rec["cards"] if a.startswith("treated"))
        recs.append((rec, bases, treats))
    n_b = min(len(b) for _, b, _ in recs)
    n_t = min(len(t) for _, _, t in recs)

    # M5: per-item loss rates over every ordered pair.
    per_target = []
    for rec, bases, treats in recs:
        c = rec["cards"]
        bb_q = bb_l = bt_q = bt_l = 0
        for a in bases:
            for b in bases:
                if a != b:
                    q, lost = _losses(c[a], c[b])
                    bb_q, bb_l = bb_q + q, bb_l + lost
            for t in treats:
                q, lost = _losses(c[a], c[t])
                bt_q, bt_l = bt_q + q, bt_l + lost
        per_target.append(
            {
                "tid": rec["tid"],
                "class": rec["corpus_class"],
                "quoted_per_baseline": [len(_quoted(c[a])) for a in bases],
                "quoted_per_treated": [len(_quoted(c[t])) for t in treats],
                "bb": (bb_q, bb_l),
                "bt": (bt_q, bt_l),
            }
        )

    def excess(rows: list[dict]) -> tuple[float, float, float, float]:
        bbq = sum(r["bb"][0] for r in rows)
        bbl = sum(r["bb"][1] for r in rows)
        btq = sum(r["bt"][0] for r in rows)
        btl = sum(r["bt"][1] for r in rows)
        noise = bbl / bbq if bbq else 0.0
        treat = btl / btq if btq else 0.0
        per_run = sum(statistics.mean(r["quoted_per_baseline"]) for r in rows)
        return noise, treat, per_run, (treat - noise) * per_run

    noise, treat, per_run, displaced = excess(per_target)
    rnd = random.Random(args.seed)
    boots = sorted(
        excess([rnd.choice(per_target) for _ in per_target])[3]
        for _ in range(BOOTSTRAP)
    )
    lo, hi = boots[int(0.025 * BOOTSTRAP)], boots[int(0.975 * BOOTSTRAP) - 1]
    m5_pass = displaced <= 1
    distinguishable = lo > 0

    # M2 over passes (thread targets).
    m2 = []
    for rec, bases, treats in recs:
        if rec["corpus_class"] != "thread":
            continue
        c = rec["cards"]
        bi = [_n_items(c[a]) for a in bases]
        ti = [_n_items(c[t]) for t in treats]
        bc = [_card_chars(c[a]) for a in bases]
        tc = [_card_chars(c[t]) for t in treats]
        mb = statistics.mean(bc)
        m2.append(
            {
                "tid": rec["tid"],
                "items_b": bi,
                "items_t": ti,
                "items_delta": statistics.mean(ti) - statistics.mean(bi),
                "items_noise": _mean_abs_pair(bi),
                "chars_delta_pct": (statistics.mean(tc) - mb) / mb * 100,
                "chars_noise_pct": _mean_abs_pair(bc) / mb * 100,
            }
        )
    med = {
        k: statistics.median(x[k] for x in m2)
        for k in ("items_delta", "items_noise", "chars_delta_pct", "chars_noise_pct")
    }
    m2_items_pass = med["items_delta"] <= 1
    m2_chars_pass = med["chars_delta_pct"] - med["chars_noise_pct"] <= 15

    # Gain per treated pass, and the baselines as a control for the matcher.
    gain_rows = []
    for rec, bases, treats in recs:
        if rec["corpus_class"] != "thread":
            continue
        gain_rows.append(
            {
                "tid": rec["tid"],
                "treated": [_gain_hit(rec["cards"][t], rec["texts"]) for t in treats],
                "baseline": [_gain_hit(rec["cards"][a], rec["texts"]) for a in bases],
            }
        )
    per_pass = [sum(r["treated"][k] for r in gain_rows) for k in range(n_t)]
    any_pass = sum(any(r["treated"]) for r in gain_rows)
    two_pass = sum(sum(r["treated"]) >= 2 for r in gain_rows)
    base_hits = sum(any(r["baseline"]) for r in gain_rows)
    gain_pass = statistics.mean(per_pass) >= 5

    spent = Ledger(LEDGER, CAP_USD).total()

    def yn(ok: bool) -> str:
        return "**PASS**" if ok else "**FAIL**"

    sec = [
        REPEATS_START,
        "## Repeats (owner's request: more passes before any rule change)",
        "",
        f"{n_b} baseline passes and {n_t} treated passes per target, same corpus, model "
        f"`{PROVIDER}/{MODEL}` and pinned worktree `0eb814c`; ledger total "
        f"**${spent:.4f} of ${CAP_USD:.2f}**. These numbers supersede the single-pass "
        "M2, M5 and Gain above; M1, M3, M4 and M6 are unchanged.",
        "",
        "### M5 - displacement over every pair",
        "",
        "Loss rate = the share of a baseline pass's `quoted` items with no match "
        "(fuzzy text or shared quote) in another card. Noise floor = over every ordered "
        f"pair of the {n_b} baselines; treated = over every (baseline, treated) pair. "
        "Excess displaced items per run = (treated rate - noise rate) x the corpus's "
        "mean quoted items per baseline run.",
        "",
        f"- Baseline-vs-baseline loss rate (noise floor): **{noise:.1%}**",
        f"- Baseline-vs-treated loss rate: **{treat:.1%}**",
        f"- Mean quoted items per baseline run across the corpus: {per_run:.1f}",
        f"- Excess displaced quoted items per treated run: **{displaced:+.2f}** "
        f"(bootstrap over targets, {BOOTSTRAP} resamples, 95 % interval "
        f"[{lo:+.2f}, {hi:+.2f}])",
        f"- Bar (at most 1 across the corpus): {yn(m5_pass)}. Distinguishable from "
        f"noise: **{'yes' if distinguishable else 'no'}** (the interval "
        f"{'excludes' if distinguishable else 'includes'} 0).",
        "",
        "| Target | Class | quoted per baseline | quoted per treated | b-b lost / compared | b-t lost / compared |",
        "|---|---|---|---|---|---|",
    ]
    for r in per_target:
        if not (r["bb"][0] or r["bt"][0]):
            continue
        sec.append(
            f"| {r['tid']} | {r['class']} | {r['quoted_per_baseline']} | "
            f"{r['quoted_per_treated']} | {r['bb'][1]} / {r['bb'][0]} | "
            f"{r['bt'][1]} / {r['bt'][0]} |"
        )
    sec += [
        "",
        "Targets with no quoted item in any baseline pass are left out of the table.",
        "",
        "### M2 - card size over passes (thread targets)",
        "",
        "Delta = mean of the treated passes minus mean of the baseline passes; noise = "
        "mean absolute difference between baseline passes.",
        "",
        f"- Items: median delta {med['items_delta']:+.2f} (noise median "
        f"{med['items_noise']:.2f}); bar +1 or less: {yn(m2_items_pass)}",
        f"- Characters: median delta {med['chars_delta_pct']:+.1f} % (noise median "
        f"{med['chars_noise_pct']:.1f} %; delta - noise "
        f"{med['chars_delta_pct'] - med['chars_noise_pct']:+.1f} %); bar +15 % or less "
        f"over the noise floor: {yn(m2_chars_pass)}",
        "",
        "| Target | Items per baseline | Items per treated | Chars delta % | Chars noise % |",
        "|---|---|---|---|---|",
    ]
    for x in m2:
        sec.append(
            f"| {x['tid']} | {x['items_b']} | {x['items_t']} | "
            f"{x['chars_delta_pct']:+.1f} | {x['chars_noise_pct']:.1f} |"
        )
    sec += [
        "",
        "### Gain over passes",
        "",
        f"- Thread targets with a thread-sourced item, per treated pass: "
        f"{', '.join(map(str, per_pass))} (mean {statistics.mean(per_pass):.1f}, "
        f"min {min(per_pass)}, max {max(per_pass)}); bar at least 5 of 15 (on the "
        f"mean): {yn(gain_pass)}",
        f"- In at least one pass: {any_pass} of {len(gain_rows)}; in at least two: "
        f"{two_pass}.",
        f"- Matcher control: baseline passes (which never see a thread) hit on "
        f"{base_hits} targets.",
        "",
        "| Target | Treated passes with a thread-sourced item |",
        "|---|---|",
    ]
    for r in gain_rows:
        sec.append(f"| {r['tid']} | {sum(r['treated'])} of {n_t} |")
    sec += ["", REPEATS_END]
    report = EVAL_DIR / "part-a-report.md"
    text = report.read_text()
    block = "\n".join(sec)
    if REPEATS_START in text:
        head, rest = text.split(REPEATS_START, 1)
        text = head + block + rest.split(REPEATS_END, 1)[1]
    else:
        text = text.rstrip("\n") + "\n\n" + block + "\n"
    report.write_text(text)
    print(
        json.dumps(
            {
                "passes": [n_b, n_t],
                "m5": {
                    "noise": noise,
                    "treated": treat,
                    "per_run": per_run,
                    "excess": displaced,
                    "ci": [lo, hi],
                    "pass": m5_pass,
                    "distinguishable": distinguishable,
                },
                "m2": {**med, "items_pass": m2_items_pass, "chars_pass": m2_chars_pass},
                "gain": {"per_pass": per_pass, "any": any_pass, "two": two_pass},
                "spend": spent,
            },
            indent=1,
        )
    )


# ---------------------------------------------------------------------------
# rerun: one model's baseline x2 + treated arms, measured, with blind sheets
# ---------------------------------------------------------------------------


def _render_card(card: dict) -> list[str]:
    """A card as the blind sheets show it (no arm, no model)."""
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


def _code_block(rec: dict) -> list[str]:
    tgt = rec["target"]
    return [
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
    ]


def cmd_rerun(env: Env, args: argparse.Namespace) -> None:
    """Measure the ``<prefix>baseline1/2`` and ``<prefix>treated`` arms; write blind sheets.

    Writes ``part-a-m4-sheet-<suffix>.md``, ``part-a-m6-sheet-<suffix>.md`` (both
    baseline-vs-baseline and baseline-vs-treated pairs, shuffled, pair type
    hidden), ``part-a-m6-key-<suffix>.json`` and a section of
    ``part-a-report.md``.
    """
    b1, b2, tr = (f"{args.prefix}{a}" for a in ("baseline1", "baseline2", "treated"))
    recs = []
    for path in sorted(CARDS.glob("T*.json")):
        rec = json.loads(path.read_text())
        if all(a in rec["cards"] for a in (b1, b2, tr)):
            recs.append(rec)
    if not recs:
        raise SystemExit(f"no target has all of {b1}, {b2}, {tr}")
    thread = [r for r in recs if r["corpus_class"] == "thread"]
    m1 = {r["tid"]: r for r in json.loads((WORK / "m1.json").read_text())}

    # Prompt identity with the first run (M1 does not depend on the model).
    same_prompt = sum(
        1
        for r in recs
        if r["prompts"].get(tr, {}).get("user")
        == r["prompts"].get("treated", {}).get("user")
        and r["prompts"].get(b1, {}).get("user")
        == r["prompts"].get("baseline1", {}).get("user")
    )
    m1_fail = [f"{r['tid']}: {'; '.join(_code_checks_arms(r, b1, tr))}" for r in recs]
    m1_fail = [f for f in m1_fail if not f.endswith(": ")]

    # M2.
    m2 = []
    for r in thread:
        c = r["cards"]
        mean_items = (_n_items(c[b1]) + _n_items(c[b2])) / 2
        mean_chars = (_card_chars(c[b1]) + _card_chars(c[b2])) / 2
        m2.append(
            {
                "tid": r["tid"],
                "items": (_n_items(c[b1]), _n_items(c[b2]), _n_items(c[tr])),
                "chars": (_card_chars(c[b1]), _card_chars(c[b2]), _card_chars(c[tr])),
                "items_delta": _n_items(c[tr]) - mean_items,
                "items_noise": abs(_n_items(c[b2]) - _n_items(c[b1])),
                "chars_delta_pct": (_card_chars(c[tr]) - mean_chars) / mean_chars * 100,
                "chars_noise_pct": abs(_card_chars(c[b2]) - _card_chars(c[b1]))
                / mean_chars
                * 100,
            }
        )
    med = {
        k: statistics.median(x[k] for x in m2)
        for k in ("items_delta", "items_noise", "chars_delta_pct", "chars_noise_pct")
    }
    m2_items_pass = med["items_delta"] <= 1
    m2_chars_pass = med["chars_delta_pct"] - med["chars_noise_pct"] <= 15

    # M5, pairwise (both directions), with a bootstrap over targets.
    rows = []
    for r in thread:
        c = r["cards"]
        bbq, bbl = (sum(x) for x in zip(_losses(c[b1], c[b2]), _losses(c[b2], c[b1])))
        btq, btl = (sum(x) for x in zip(_losses(c[b1], c[tr]), _losses(c[b2], c[tr])))
        rows.append(
            {
                "tid": r["tid"],
                "quoted": (
                    len(_quoted(c[b1])),
                    len(_quoted(c[b2])),
                    len(_quoted(c[tr])),
                ),
                "bb": (bbq, bbl),
                "bt": (btq, btl),
            }
        )

    def excess(sample: list[dict]) -> tuple[float, float, float, float]:
        bbq = sum(x["bb"][0] for x in sample)
        bbl = sum(x["bb"][1] for x in sample)
        btq = sum(x["bt"][0] for x in sample)
        btl = sum(x["bt"][1] for x in sample)
        noise = bbl / bbq if bbq else 0.0
        treat = btl / btq if btq else 0.0
        per_run = sum((x["quoted"][0] + x["quoted"][1]) / 2 for x in sample)
        return noise, treat, per_run, (treat - noise) * per_run

    noise, treat, per_run, displaced = excess(rows)
    rnd = random.Random(args.seed)
    boots = sorted(
        excess([rnd.choice(rows) for _ in rows])[3] for _ in range(BOOTSTRAP)
    )
    lo, hi = boots[int(0.025 * BOOTSTRAP)], boots[int(0.975 * BOOTSTRAP) - 1]
    m5_pass = displaced <= 1
    distinguishable = lo > 0

    # Gain, and the matcher control on the baselines.
    gain = {r["tid"]: _gain_hit(r["cards"][tr], r["texts"]) for r in thread}
    gain_base = sum(
        _gain_hit(r["cards"][b1], r["texts"]) or _gain_hit(r["cards"][b2], r["texts"])
        for r in thread
    )
    gain_n = sum(gain.values())
    gain_pass = gain_n >= 5

    # Unit disagreement (the noise of M4).
    b2_units = b2_new = t_units = t_new = 0
    for r in thread:
        c = r["cards"]
        u1, u2, ut = _units(c[b1]), _units(c[b2]), _units(c[tr])
        b2_units += len(u2)
        b2_new += sum(1 for u in u2 if not _present(u, u1))
        t_units += len(ut)
        t_new += sum(1 for u in ut if not _present(u, u1 + u2))

    # M4 sheet.
    m4 = [
        f"# Plan 62 Part A - M4 precision sheet, `{args.suffix}` run (judge, by hand)",
        "",
        "Every item or `why` sentence of a **treated** card that is absent from **both** "
        f"baseline cards of the same run (fuzzy match, `SequenceMatcher` ratio >= "
        f"{FUZZY_SAME}, or a shared quote). Thread targets only. Fill the three verdict "
        "columns with `y` / `n` / `?`.",
        "",
        "- **True?** Is the claim true of today's code (the code block shown)?",
        "- **Rejected / open?** Is it a rejected suggestion or an open question "
        "presented as fact (`y` = yes, it is one; this must be `n`)?",
        "- **On target?** Is it about the target symbol (not other code in the file)?",
        "",
        "Bar (plan section 9): 0 rejected-suggestion constraints; at least 90 % precise "
        "(precise = True `y`, Rejected `n`, On target `y`).",
        "",
        f"Noise floor: {b2_new} of {b2_units} baseline-2 units are absent from baseline 1 "
        "under the same matcher. Units marked **thread-linked** are listed first in each "
        "target.",
        "",
    ]
    m4_count = m4_linked = 0
    for r in thread:
        c = r["cards"]
        new = [
            u for u in _units(c[tr]) if not _present(u, _units(c[b1]) + _units(c[b2]))
        ]
        if not new:
            continue
        for u in new:
            u["thread_linked"] = _thread_link(u, r)
        new.sort(key=lambda u: not u["thread_linked"])
        m4 += [
            f"## {r['tid']} - `{r['target']['qualified_name']}`",
            "",
            *_code_block(r),
            "<details><summary>Review threads the treated prompt rendered</summary>",
            "",
            "```",
            *[line for lines in r["rendered_threads"].values() for line in lines],
            "```",
            "",
            "</details>",
            "",
        ]
        for u in new:
            m4_count += 1
            m4_linked += bool(u["thread_linked"])
            origins = _quote_origins(u, r["texts"]) if u["quote"] else []
            refs = ", ".join(f"{x['kind']} {x['id'][:10]}" for x in u["refs"]) or "-"
            link = (
                f" - **thread-linked** ({u['thread_linked']})"
                if u["thread_linked"]
                else ""
            )
            m4 += [
                f"### M4-{m4_count:03d} ({u['kind']}, basis `{u['basis']}`){link}",
                "",
                f"> {u['text']}",
                "",
                f"- Refs: {refs}",
                f"- Quote: {u['quote'] or '-'}"
                + (f" (found in: {', '.join(origins)})" if origins else ""),
                "",
                "| True of today's code? | Rejected suggestion / open question? "
                "| About the target symbol? | Note |",
                "|---|---|---|---|",
                "|   |   |   |   |",
                "",
            ]
    m4.insert(4, f"Units to judge: **{m4_count}** ({m4_linked} thread-linked).")
    m4.insert(5, "")
    (EVAL_DIR / f"part-a-m4-sheet-{args.suffix}.md").write_text("\n".join(m4) + "\n")

    # M6 sheet: both pair types, shuffled, the type hidden.
    pairs = [(r, "baseline-vs-baseline", b1, b2) for r in thread] + [
        (r, "baseline-vs-treated", b1, tr) for r in thread
    ]
    rnd = random.Random(f"{args.seed}-{args.suffix}-m6")
    rnd.shuffle(pairs)
    key: dict[str, dict] = {}
    m6 = [
        f"# Plan 62 Part A - M6 blind focus comparison, `{args.suffix}` run (judge)",
        "",
        "For each pair: two cards of the same code. Which card states the target's "
        "reason more accurately and with less irrelevant content? Write `A`, `B` or `=` "
        "in the verdict line. Pairs are shuffled and some pairs compare two cards made "
        f"the same way. The key is `part-a-m6-key-{args.suffix}.json`: do not open it "
        "until every verdict is in.",
        "",
    ]
    for n, (r, kind, left, right) in enumerate(pairs, 1):
        sides = [left, right]
        rnd.shuffle(sides)
        label = f"P{n:02d}"
        key[label] = {"tid": r["tid"], "pair": kind, "A": sides[0], "B": sides[1]}
        m6 += [
            f"## {label} - `{r['target']['qualified_name']}`",
            "",
            *_code_block(r),
            "### Card A",
            "",
            *_render_card(r["cards"][sides[0]]),
            "### Card B",
            "",
            *_render_card(r["cards"][sides[1]]),
            "**Verdict (A / B / =):** ",
            "",
            "---",
            "",
        ]
    (EVAL_DIR / f"part-a-m6-sheet-{args.suffix}.md").write_text("\n".join(m6) + "\n")
    (EVAL_DIR / f"part-a-m6-key-{args.suffix}.json").write_text(
        json.dumps(key, indent=1) + "\n"
    )

    # Spend of this run.
    spend: Counter = Counter()
    for line in LEDGER.read_text().splitlines():
        if line.strip():
            e = json.loads(line)
            if str(e.get("arm", "")).startswith(args.prefix):
                spend[e["arm"]] += float(e.get("cost") or 0)
    total = Ledger(LEDGER, CAP_USD).total()

    def yn(ok: bool) -> str:
        return "**PASS**" if ok else "**FAIL**"

    start, end = f"<!-- {args.suffix}:start -->", f"<!-- {args.suffix}:end -->"
    sec = [
        start,
        f"## {args.title}",
        "",
        f"Model `{PROVIDER}/{MODEL}`, thread targets only ({len(thread)}), arms "
        f"`{b1}`, `{b2}` and `{tr}` (one pass each), same pinned worktree and DB copies "
        "as the Haiku runs; no new commit description was made (the cached ones apply). "
        "Spend of this run: "
        + ", ".join(f"{k} ${v:.4f}" for k, v in sorted(spend.items()))
        + f" (**${sum(spend.values()):.4f}**); ledger total **${total:.4f} of "
        f"${CAP_USD:.2f}**.",
        "",
        "### M1",
        "",
        f"The prompts do not depend on the model (one default prompt for every model): "
        f"{same_prompt} of {len(recs)} targets' baseline-1 and treated prompts are "
        "byte-identical to the Haiku run's, so M1 above stands (thread targets median "
        f"+{statistics.median(m1[r['tid']]['delta'] for r in thread):.0f} characters). "
        "Code checks on these prompts: "
        + ("**all hold**." if not m1_fail else "**failing**: " + "; ".join(m1_fail)),
        "",
        "### M2 - card size",
        "",
        f"- Items: median delta {med['items_delta']:+.1f} (noise median "
        f"{med['items_noise']:.1f}); bar +1 or less: {yn(m2_items_pass)}",
        f"- Characters: median delta {med['chars_delta_pct']:+.1f} % (noise median "
        f"{med['chars_noise_pct']:.1f} %; delta - noise "
        f"{med['chars_delta_pct'] - med['chars_noise_pct']:+.1f} %); bar +15 % or less "
        f"over the noise floor: {yn(m2_chars_pass)}",
        "",
        "| Target | Items b1 / b2 / t | Chars b1 / b2 / t | Chars delta % |",
        "|---|---|---|---|",
        *[
            f"| {x['tid']} | {' / '.join(map(str, x['items']))} | "
            f"{' / '.join(map(str, x['chars']))} | {x['chars_delta_pct']:+.1f} |"
            for x in m2
        ],
        "",
        "### M5 - displacement (pairwise)",
        "",
        f"- Baseline-vs-baseline loss rate (noise floor, both directions): **{noise:.1%}**",
        f"- Baseline-vs-treated loss rate (both baselines against treated): **{treat:.1%}**",
        f"- Mean quoted items per baseline run (thread targets): {per_run:.1f}",
        f"- Excess displaced quoted items per treated run: **{displaced:+.2f}** "
        f"(bootstrap over targets, {BOOTSTRAP} resamples, 95 % interval "
        f"[{lo:+.2f}, {hi:+.2f}])",
        f"- Bar (at most 1): {yn(m5_pass)}. Distinguishable from noise: "
        f"**{'yes' if distinguishable else 'no'}**.",
        "",
        "| Target | quoted b1 / b2 / t | b-b lost / compared | b-t lost / compared |",
        "|---|---|---|---|",
        *[
            f"| {x['tid']} | {' / '.join(map(str, x['quoted']))} | "
            f"{x['bb'][1]} / {x['bb'][0]} | {x['bt'][1]} / {x['bt'][0]} |"
            for x in rows
        ],
        "",
        "### Gain",
        "",
        f"- {gain_n} of {len(thread)} thread targets have a treated item quoted from (or "
        f"citing the text of) a PR-author thread comment; bar at least 5: {yn(gain_pass)}",
        f"- Targets: {', '.join(t for t, hit in gain.items() if hit) or '-'}",
        f"- Matcher control: the baselines hit on {gain_base} targets.",
        "",
        "### Noise of the card text",
        "",
        f"- Baseline-1 vs baseline-2 unit disagreement: **{b2_new} of {b2_units} "
        f"({b2_new / b2_units:.0%})** baseline-2 units have no match in baseline 1 "
        "(Haiku: 137 of 156, 88 %).",
        f"- Treated units absent from both baselines: {t_new} of {t_units} "
        f"({t_new / t_units:.0%}).",
        "",
        "### Sheets for the judge (blind)",
        "",
        f"- `plans/62-eval/part-a-m4-sheet-{args.suffix}.md`: {m4_count} units "
        f"({m4_linked} thread-linked).",
        f"- `plans/62-eval/part-a-m6-sheet-{args.suffix}.md`: {len(pairs)} shuffled "
        f"pairs, {len(thread)} baseline-vs-baseline and {len(thread)} "
        "baseline-vs-treated, sides randomized, pair type hidden; key "
        f"`plans/62-eval/part-a-m6-key-{args.suffix}.json`. Bar: treated worse on at "
        "most 2 of 15; the baseline-vs-baseline pairs give the judge's own noise rate.",
        "",
        end,
    ]
    report = EVAL_DIR / "part-a-report.md"
    text = report.read_text()
    block = "\n".join(sec)
    if start in text:
        head, rest = text.split(start, 1)
        text = head + block + rest.split(end, 1)[1]
    else:
        text = text.rstrip("\n") + "\n\n" + block + "\n"
    report.write_text(text)
    print(
        json.dumps(
            {
                "targets": len(thread),
                "same_prompt": same_prompt,
                "m1_failures": m1_fail,
                "m2": {**med, "items_pass": m2_items_pass, "chars_pass": m2_chars_pass},
                "m5": {
                    "noise": noise,
                    "treated": treat,
                    "per_run": per_run,
                    "excess": displaced,
                    "ci": [lo, hi],
                    "pass": m5_pass,
                    "distinguishable": distinguishable,
                },
                "gain": [gain_n, gain_pass, gain_base],
                "unit_noise": [b2_new, b2_units],
                "m4_units": [m4_count, m4_linked],
                "spend": dict(spend),
                "ledger": total,
            },
            indent=1,
        )
    )


def _code_checks_arms(record: dict, base: str, treated: str) -> list[str]:
    """:func:`_code_checks` on a named pair of arms."""
    prompts = record["prompts"]
    view = {
        "corpus_class": record["corpus_class"],
        "prompts": {"baseline1": prompts.get(base), "treated": prompts.get(treated)},
    }
    return _code_checks(view)


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Part C grounding arms (plan 62 section 14.8, R48, step C7)
# ---------------------------------------------------------------------------

C_CARDS = EVAL_DIR / "part-c-cards"
C_WORK = EVAL_DIR / "part-c-work"


def _cards_dir(level: str) -> Path:
    """Where one level's Part C cards live (``medium``: the grounding arms' dir)."""
    return C_CARDS if level == "medium" else EVAL_DIR / f"part-c-cards-{level}"


GROUNDING_ARMS = ("g0a", "g0b", "g1", "g2")
"""G0 plain twice (the noise control), G1 = G0 + R48 (b)-(d), G2 = G1 + (a)."""
GROUNDING_LEVEL = "medium"
LEVELS_OFFERED = ("low", "medium")
"""The levels R49 leaves (``high`` and the verification pass are gone)."""
LEVEL_ARMS = ("g2", "g2b")
"""The ``low`` level arm twice (G2's prompt), the second as the noise control."""
GROUNDING_CAP_USD = 10.0
"""The grounding arms are funded from Part A's unspent budget (R48)."""
PRE_R48_REV = "1ae370e^"
"""The commit before step C3a: the plain (G0) system prompt and labels."""
CHARS_PER_TOKEN = 2.4
"""Conservative prompt characters per token (measured 2.43-2.46 on biome)."""
EST_OUT_TOKENS = 1500
GUARD_OUT_TOKENS = 2500
"""Output tokens the pre-call cap check assumes (the Sonnet cards' max ~1,700)."""
CLAIM_SHEETS = 3
BIOME_REPO = "biome at `~/.local/state/whygraph-dev/repos/biome`"
"""How the sheets name the biome checkout the judges read."""

_RG_PROMPTS = "src/whygraph/analyze/prompts/rationale_generator/default"
_OLD_CALLERS = "Called by ({n} caller(s) — blast radius of a change):"
_OLD_CALLEES = "Calls ({n} callee(s) — what this depends on):"
_OLD_PR_ORIGIN = "original commit recovered from a squash-merged PR"
_NEW_CALLERS_RE = re.compile(
    r"^Possible callers \((\d+), matched by name; "
    r"may include other functions with the same name\):$",
    re.M,
)
_NEW_CALLEES_RE = re.compile(r"^Possible callees \((\d+), matched by name\):$", re.M)
_SOURCE_BLOCK_RE = re.compile(
    r"TARGET SOURCE \([^\n]*, as of the card's build\)\n\n(?:[ \d]+  [^\n]*(?:\n|$))*\n?"
)


def _git_show(rev: str, path: str) -> str:
    return subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _pre_r48_prompt() -> Any:
    """The G0 prompt pair: the pre-C3a ``system.md`` and today's (unchanged) ``task.md``."""
    from whygraph.analyze.prompt import Prompt

    task = (REPO / _RG_PROMPTS / "task.md").read_text(encoding="utf-8")
    if _git_show(PRE_R48_REV, f"{_RG_PROMPTS}/task.md") != task:
        raise SystemExit("task.md changed since the pre-R48 commit; G0 is not plain")
    old_module = _git_show(PRE_R48_REV, "src/whygraph/analyze/rationale_generator.py")
    for literal in (
        "Called by ({len(context.callers)} caller(s) — blast radius of a change):",
        "Calls ({len(context.callees)} callee(s) — what this depends on):",
        f'"pr-origin": "{_OLD_PR_ORIGIN}"',
    ):
        if literal not in old_module:
            raise SystemExit(f"pre-R48 label not found in {PRE_R48_REV}: {literal}")
    return Prompt(system=_git_show(PRE_R48_REV, f"{_RG_PROMPTS}/system.md"), task=task)


def _to_old_labels(text: str) -> str:
    """``text`` with the R48 (c) / (d) labels put back to the pre-C3a ones."""
    from whygraph.analyze.rationale_generator import _SOURCE_LABELS

    text = _NEW_CALLERS_RE.sub(lambda m: _OLD_CALLERS.format(n=m[1]), text)
    text = _NEW_CALLEES_RE.sub(lambda m: _OLD_CALLEES.format(n=m[1]), text)
    return text.replace(_SOURCE_LABELS["pr-origin"], _OLD_PR_ORIGIN)


class _Pre48Labels:
    """Patch the generator module's graph headers and draft label to the G0 ones."""

    def __enter__(self) -> None:
        from whygraph.analyze import rationale_generator as rg

        self._fmt = rg._format_symbol_context
        self._label = rg._SOURCE_LABELS["pr-origin"]
        new_fmt = self._fmt
        rg._format_symbol_context = lambda context: _to_old_labels(new_fmt(context))
        rg._SOURCE_LABELS["pr-origin"] = _OLD_PR_ORIGIN

    def __exit__(self, *exc: object) -> None:
        from whygraph.analyze import rationale_generator as rg

        rg._format_symbol_context = self._fmt
        rg._SOURCE_LABELS["pr-origin"] = self._label


class _NoPatch:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


def _arm_generator(arm: str, client: Any, config: Any, old_prompt: Any) -> Any:
    """The arm's generator: G0 the pre-R48 prompt, G1 today's without the source, G2 (and G2b) today's."""
    from whygraph.analyze import RationaleGenerator
    from whygraph.analyze.rationale_generator import _PrRenderCaps

    return RationaleGenerator(
        client,
        timeout_sec=config.timeout_for("rationale"),
        caps=_PrRenderCaps.from_config(config.rationale),
        rationale_prompt=old_prompt if arm.startswith("g0") else None,
        target_source=arm.startswith("g2"),
    )


def _arm_patch(arm: str) -> Any:
    return _Pre48Labels() if arm.startswith("g0") else _NoPatch()


def _coverage_dict(coverage: Any) -> dict | None:
    import dataclasses

    if coverage is None:
        return None
    out = dataclasses.asdict(coverage)
    out["omitted_shas"] = sorted(out.get("omitted_shas") or ())
    out["trimmed"] = list(out.get("trimmed") or ())
    return out


def _c_card_dict(r: Any) -> dict:
    card = _card_dict(r)
    card["coverage"] = _coverage_dict(r.coverage)
    return card


def _grounding_checks(record: dict) -> list[str]:
    """Failed prompt checks: each arm differs from the one below it by its part only."""
    p = record["prompts"]
    fails: list[str] = []
    if p["g0a"] != p["g0b"]:
        fails.append("g0a and g0b prompts differ")
    if p["g0a"]["system"] == p["g1"]["system"]:
        fails.append("g0 and g1 system prompts are the same")
    if p["g1"]["system"] != p["g2"]["system"]:
        fails.append("g1 and g2 system prompts differ")
    if _to_old_labels(p["g1"]["user"]) != p["g0a"]["user"]:
        fails.append("g1 user prompt differs from g0 by more than the labels")
    source = (record["coverage"]["g2"] or {}).get("source")
    g2_user = p["g2"]["user"]
    if source == "included":
        stripped, n = _SOURCE_BLOCK_RE.subn("", g2_user, count=1)
        if n != 1 or stripped != p["g1"]["user"]:
            fails.append("g2 user prompt differs from g1 by more than the source block")
    elif g2_user != p["g1"]["user"]:
        fails.append(f"g2 ({source}) user prompt differs from g1")
    return fails


def _target_inputs(row: dict, config: Any, level: str) -> tuple[Any, dict, dict]:
    """One target's evidence, ``generate`` keywords and record fields, as the card path builds them.

    Mirrors :func:`whygraph.mcp.rationale.rationale_card` at ``level``: the
    bundle, the description backfill (which describes nothing new here: the
    corpus was described in Part A), the review scope and the symbol context.
    Runs under the bound project.
    """
    from whygraph.analyze import card_bundle_for
    from whygraph.db import get_session
    from whygraph.mcp.evidence import (
        backfill_evidence_descriptions,
        collect_evidence,
        review_scope,
    )
    from whygraph.mcp.rationale import _missing_reasons, _symbol_context
    from whygraph.mcp.targets import repo_root

    target = _target_of(Node(**{k: row[k] for k in Node.__annotations__}))
    evidence = collect_evidence(target, limit=20)
    bundle = card_bundle_for(evidence, config.rationale, level)
    items = list(bundle.items)
    outcome = backfill_evidence_descriptions(
        items,
        target_path=target.path,
        allow=lambda: False,  # the corpus is described; no new describe
        max_commits=len(items),
    )
    with get_session() as session:
        scope = review_scope(session, repo_root(), target.path)
    rng = (target.line_start, target.line_end)
    lines = scope.current_lines or ()
    code = "\n".join(lines[rng[0] - 1 : rng[1]])
    kwargs: dict[str, Any] = {
        "symbol_context": _symbol_context(target),
        "subject": target.qualified_name,
        "bundle": bundle,
        "missing_descriptions": _missing_reasons(outcome),
        "target_path": target.path,
        "paths": scope.paths,
        "file_lines": scope.current_lines,
        "target_range": rng,
    }
    fields = {
        "tid": row["tid"],
        "target": {k: row[k] for k in ("qualified_name", "path", "start", "end")},
        "level": level,
        "evidence_commits": len(evidence),
        "bundle_commits": len(items),
        "descriptions_waiting": sorted(outcome.waiting),
        "code": code,
        "code_chars": len(code),
    }
    return evidence, kwargs, fields


def cmd_level_prompts(env: Env, args: argparse.Namespace) -> None:
    """Level-arm prompt sizes, the ``medium`` reuse check and the estimate (no LLM).

    Builds G2's draft prompt at ``low`` and ``medium`` for each thread target
    and compares today's ``medium`` prompt with the one the stored G2 card was
    made from (``part-c-cards``): equal means the G2 cards can stand in for
    ``medium``. The estimate covers ``LEVEL_ARMS`` at ``low``, plus ``medium``
    for the targets whose prompt changed.
    """
    from whygraph.core import get_config
    from whygraph.core.context import use_project

    corpus = [
        r
        for r in json.loads((EVAL_DIR / "part-a-corpus.json").read_text())
        if r["corpus_class"] == "thread"
    ]
    price_in, price_out = _price_per_m()
    v2, cg = WORK / "v2" / "whygraph.db", WORK / "cg" / "codegraph.db"
    ctx = eval_context(env, v2, cg)
    old_prompt = _pre_r48_prompt()
    sizes: dict[str, dict] = {}
    cost: Counter = Counter()
    changed: list[str] = []
    with use_project(ctx):
        config = get_config()
        for row in corpus:
            entry = sizes.setdefault(row["tid"], {})
            prompts = {}
            for level in ("low", "medium"):
                evidence, kwargs, _ = _target_inputs(row, config, level)
                client = PromptOnlyClient()
                _arm_generator("g2", client, config, old_prompt).generate(
                    evidence, **kwargs
                )
                assert client.last_prompt is not None
                prompts[level] = client.last_prompt
                entry[level] = sum(len(v) for v in client.last_prompt.values())
            est = (
                entry["low"] / CHARS_PER_TOKEN * price_in + EST_OUT_TOKENS * price_out
            ) / 1e6
            cost["low"] += est * len(LEVEL_ARMS)
            stored_path = C_CARDS / f"{row['tid']}.json"
            stored = (
                json.loads(stored_path.read_text()).get("prompts", {}).get("g2")
                if stored_path.exists()
                else None
            )
            entry["medium_equals_g2"] = stored == prompts["medium"]
            entry["low_equals_medium"] = prompts["low"] == prompts["medium"]
            if not entry["medium_equals_g2"]:
                changed.append(row["tid"])
                cost["medium"] += (
                    entry["medium"] / CHARS_PER_TOKEN * price_in
                    + EST_OUT_TOKENS * price_out
                ) / 1e6
            print(f"{row['tid']} {entry}")
    C_WORK.mkdir(parents=True, exist_ok=True)
    out = {
        "arm": "g2",
        "model": f"{PROVIDER}/{MODEL}",
        "sizes": sizes,
        "medium_changed": changed,
        "estimate_usd": {k: round(v, 4) for k, v in cost.items()},
        "assumptions": {
            "chars_per_token": CHARS_PER_TOKEN,
            "draft_output_tokens": EST_OUT_TOKENS,
            "low_arms": list(LEVEL_ARMS),
        },
    }
    (C_WORK / "level-prompts.json").write_text(json.dumps(out, indent=1))
    print(f"medium prompt changed since the G2 cards on: {changed or 'none'}")
    print(json.dumps(out["estimate_usd"]))


def cmd_grounding(env: Env, args: argparse.Namespace) -> None:
    """Build the arms' prompts (no LLM); with ``--yes``, generate the cards.

    At ``--level medium`` (the default) these are the grounding arms; at
    another level, the level arms of section 14.8 (one arm, the surviving
    prompt), stored in their own directory.
    """
    global STEP
    level = args.level
    corpus_name = args.corpus
    if corpus_name == "biome":
        STEP = (
            "plan62-part-c-grounding" if level == "medium" else "plan62-part-c-levels"
        )
        cards_dir = _cards_dir(level)
    else:
        STEP = f"plan62-part-c-{corpus_name}-levels"
        cards_dir = _ext_cards_dir(corpus_name, level)
    from whygraph.analyze import RationaleGenerator
    from whygraph.core import get_config
    from whygraph.core.context import use_project
    from whygraph.core.usage import use_usage_sink

    if corpus_name == "biome":
        corpus = [
            r
            for r in json.loads((EVAL_DIR / "part-a-corpus.json").read_text())
            if r["corpus_class"] == "thread"
        ]
        allowed = GROUNDING_ARMS if level == "medium" else LEVEL_ARMS
    else:
        corpus = json.loads((_ext_dir(corpus_name) / "targets.json").read_text())
        allowed = ("g2",) if level == "medium" else LEVEL_ARMS
    arms = tuple(
        a.strip() for a in (args.arms or ",".join(allowed)).split(",") if a.strip()
    )
    unknown = set(arms) - set(allowed)
    if unknown:
        raise SystemExit(f"unknown arms {sorted(unknown)} at {level}")
    ledger = Ledger(LEDGER, args.cap, reserve=0.0)
    price_in, price_out = _price_per_m()
    print(f"corpus {len(corpus)} targets, arms {arms}, level {level}")
    print(f"ledger ${ledger.total():.4f}; cap ${args.cap:.2f}")
    if corpus_name == "biome":
        v2, cg = WORK / "v2" / "whygraph.db", WORK / "cg" / "codegraph.db"
        ctx = eval_context(env, v2, cg)
    else:
        ctx = _ext_context(env, corpus_name)
    old_prompt = _pre_r48_prompt()
    cards_dir.mkdir(parents=True, exist_ok=True)
    portal_sink, writer = (None, None)
    # An external corpus is no portal project: its spend goes to the eval ledger only.
    if args.yes and not args.no_portal_ledger and corpus_name == "biome":
        portal_sink, writer = _portal_sink(env, ctx.config, ctx.key_scopes)
    sink = EvalSink(ledger, ctx.config, portal_sink)
    estimate = 0.0
    pending = 0
    try:
        with use_project(ctx):
            config = get_config()
            real = RationaleGenerator.from_config(config)._client if args.yes else None
            for row in corpus:
                tid = row["tid"]
                out = cards_dir / f"{tid}.json"
                record = json.loads(out.read_text()) if out.exists() else {}
                evidence, kwargs, fields = _target_inputs(row, config, level)
                record.update(fields)
                prompts: dict[str, dict] = {}
                coverage: dict[str, dict | None] = {}
                for arm in arms:
                    client = PromptOnlyClient()
                    with _arm_patch(arm):
                        draft = _arm_generator(
                            arm, client, config, old_prompt
                        ).generate(evidence, **kwargs)
                    assert client.last_prompt is not None
                    prompts[arm] = client.last_prompt
                    coverage[arm] = _coverage_dict(draft.coverage)
                drift = [
                    a
                    for a in prompts
                    if a in record.get("cards", {})
                    and record.get("prompts", {}).get(a) != prompts[a]
                ]
                if drift:
                    raise SystemExit(
                        f"{tid}: today's prompt differs from the one the stored "
                        f"{drift} card(s) were made from; move the record aside "
                        "to regenerate"
                    )
                record.setdefault("prompts", {}).update(prompts)
                record["prompt_chars"] = {
                    a: len(p["system"]) + len(p["user"])
                    for a, p in record["prompts"].items()
                }
                record.setdefault("coverage", {}).update(coverage)
                record.setdefault("cards", {})
                if set(GROUNDING_ARMS) <= set(record["prompts"]):
                    record["checks_failed"] = _grounding_checks(record)
                todo = [a for a in arms if a not in record["cards"]]
                for arm in todo:
                    est = (
                        record["prompt_chars"][arm] / CHARS_PER_TOKEN * price_in
                        + EST_OUT_TOKENS * price_out
                    ) / 1e6
                    estimate += est
                    pending += 1
                pc = record["prompt_chars"]
                print(
                    f"{tid} chars "
                    + " ".join(f"{a}={pc[a]}" for a in arms)
                    + f" source={(coverage.get('g2') or {}).get('source')}"
                    + f" checks_failed={record.get('checks_failed')}"
                    + f" todo={todo}"
                )
                out.write_text(json.dumps(record, indent=1))
                if not args.yes:
                    continue
                for arm in todo:
                    guarded = GuardedClient(
                        real,
                        ledger,
                        price_in,
                        price_out,
                        max_out_tokens=GUARD_OUT_TOKENS,
                        chars_per_token=CHARS_PER_TOKEN,
                    )
                    _CURRENT_CALL.set((tid, arm))
                    card = None
                    for attempt in range(2):
                        try:
                            with _arm_patch(arm), use_usage_sink(sink):
                                card = _arm_generator(
                                    arm, guarded, config, old_prompt
                                ).generate(evidence, **kwargs)
                            break
                        except BudgetStop:
                            raise
                        except Exception as exc:  # noqa: BLE001 -- retried once, then recorded
                            print(f"  {arm} attempt {attempt + 1} failed: {exc}")
                            record.setdefault("errors", []).append(
                                {"arm": arm, "error": str(exc)}
                            )
                    if guarded.last_prompt != prompts[arm]:
                        record.setdefault("errors", []).append(
                            {
                                "arm": arm,
                                "error": "sent prompt differs from the dry one",
                            }
                        )
                    if card is not None:
                        record["cards"][arm] = _c_card_dict(card)
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
    print(
        f"estimate for the {pending} card calls this run planned: ${estimate:.4f} "
        f"({CHARS_PER_TOKEN} chars/token, {EST_OUT_TOKENS} output tokens each)"
    )
    print(f"ledger total ${ledger.total():.4f} of ${args.cap:.2f}")


_CLAIMS_RULES = """\
Fill one row per unit, in a separate file (below), with `y` / `n` / `?` in each verdict column and a short reason that names the evidence you used (a file and line of today's code, a commit, a PR).

- **T** - true of today's code and history: the claim holds for the target code shown and the repository at the stated HEAD; a history claim is true when the repo history and the evidence file agree with it. A claim that attributes content to a commit / PR / issue that you cannot find in the evidence file or the repo is `?`.
- **O** - about the target: `y` when the unit is about the target symbol itself (its behaviour, its constraints, its callers' dependence on it, its own history); `n` when it is about another function, a caller's own logic, a sibling, a test elsewhere, or the module / PR at large, even when true.
- **N** - narration: `y` when the unit only restates what the code does, with no reason from the evidence (no commit, PR, issue or review comment behind the *why*); `n` when it carries a reason from the history.
- **R** - rejected / open / retracted: `y` when the unit presents a rejected suggestion, an open review question, an uncertain answer or a statement the author took back as fact; this should be `n`.

Units of one target come from several cards of that target, shuffled; nothing on the sheet tells you which card a unit came from, and you must not try to find out (do not open any `*-key*.json` file, `part-c-cards/`, or `part-c-work/` other than the evidence files named below).
"""


def _claims_target_block(
    rec: dict, units: list[tuple[str, dict]], evidence: str = "evidence"
) -> list[str]:
    tgt = rec["target"]
    out = [f"## {rec['tid']} - `{tgt['qualified_name']}`", ""]
    out += _code_block(rec)
    out += [
        "Evidence the cards were written from: "
        f"`plans/62-eval/part-c-work/{evidence}/{rec['tid']}.txt`",
        "",
        "| Unit | Kind | Text | Refs |",
        "|---|---|---|---|",
    ]
    for uid, u in units:
        refs = ", ".join(f"{r['kind']} {r['id'][:10]}" for r in u.get("refs", []))
        text = u["text"].replace("|", "\\|").replace("\n", " ")
        out.append(f"| {uid} | {u['kind']} | {text} | {refs or '-'} |")
    out.append("")
    return out


def _balanced(sizes: dict[str, int], n: int) -> list[list[str]]:
    """Split keys into ``n`` groups of similar total size (largest first, greedy)."""
    groups: list[list[str]] = [[] for _ in range(n)]
    totals = [0] * n
    for key in sorted(sizes, key=lambda k: -sizes[k]):
        i = totals.index(min(totals))
        groups[i].append(key)
        totals[i] += sizes[key]
    return [sorted(g) for g in groups]


def _write_claims_sheets(
    entries: list[tuple[dict, dict[str, tuple[dict, dict]]]],
    *,
    prefix: str,
    title: str,
    rng: random.Random,
    head: str,
    evidence: str = "evidence",
    repo: str = BIOME_REPO,
) -> list[tuple[Path, int]]:
    """Blind claims sheets: every unit of every card, shuffled within its target.

    ``entries`` holds, per target record, its cards by a hidden label (an arm
    or a level) with their verification verdicts by claim id. Targets are
    split into :data:`CLAIM_SHEETS` sheets of similar size, each with its own
    key file (label, kind, index and any verdict per unit id).
    """
    per_target: dict[str, list[tuple[dict, dict]]] = {}
    records = [rec for rec, _ in entries]
    for rec, cards in entries:
        units: list[tuple[dict, dict]] = []
        for arm, (card, checks) in cards.items():
            counters: Counter = Counter()
            for u in _units(card):
                idx = counters[u["kind"]]
                counters[u["kind"]] += 1
                meta = {"tid": rec["tid"], "arm": arm, "kind": u["kind"], "index": idx}
                if u["kind"] in ("constraint", "risk"):
                    cid = ("C" if u["kind"] == "constraint" else "R") + str(idx + 1)
                    if cid in checks:
                        meta["check"] = checks[cid]
                units.append((u, meta))
        rng.shuffle(units)
        per_target[rec["tid"]] = units
    groups = _balanced({t: len(u) for t, u in per_target.items()}, CLAIM_SHEETS)
    by_tid = {r["tid"]: r for r in records}
    sheet_paths = []
    for n, group in enumerate(groups, start=1):
        key: dict[str, dict] = {}
        body: list[str] = []
        count = 0
        for tid in group:
            numbered = []
            for u, meta in per_target[tid]:
                count += 1
                uid = f"C{n}-{count:03d}"
                key[uid] = {**meta, "text": u["text"]}
                numbered.append((uid, u))
            body += _claims_target_block(by_tid[tid], numbered, evidence)
        sheet = EVAL_DIR / f"{prefix}-sheet-{n}.md"
        judge = EVAL_DIR / f"{prefix}-judge-{n}.md"
        header = [
            f"# Plan 62 Part C - {title} sheet {n} of {len(groups)} (judge)",
            "",
            f"Units to judge: **{count}** on {len(group)} targets "
            f"({', '.join(group)}). Repository: {repo}, HEAD `{head}` "
            "(read-only: `git log`, `git show`, `git blame`, grep, file reads; "
            "never write to it).",
            "",
            _CLAIMS_RULES,
            f"Write your verdicts to `{judge.relative_to(REPO)}` as one markdown "
            "table, `| Unit | T | O | N | R | Reason |`, one row per unit, every "
            "unit of this sheet, in sheet order.",
            "",
        ]
        sheet.write_text("\n".join(header + body))
        (EVAL_DIR / f"{prefix}-key-{n}.json").write_text(json.dumps(key, indent=1))
        sheet_paths.append((sheet, count))
    return sheet_paths


def cmd_grounding_sheets(env: Env, args: argparse.Namespace) -> None:
    """Blind claims and pairs sheets for separate judges, keys apart (no LLM)."""
    rng = random.Random(args.seed)
    records = [json.loads(p.read_text()) for p in sorted(C_CARDS.glob("T*.json"))]
    records = [r for r in records if set(GROUNDING_ARMS) <= set(r.get("cards", {}))]
    if not records:
        raise SystemExit("no target has all four grounding cards")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_biome_root(env),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    evidence_dir = C_WORK / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for rec in records:
        # The history every arm saw; G1's prompt has no source block and today's labels.
        (evidence_dir / f"{rec['tid']}.txt").write_text(rec["prompts"]["g1"]["user"])

    # (a) claims: every unit of every card, shuffled within the target.
    entries = [
        (
            rec,
            {
                arm: (rec["cards"][arm], record_checks(rec, arm))
                for arm in GROUNDING_ARMS
            },
        )
        for rec in records
    ]
    sheet_paths = _write_claims_sheets(
        entries, prefix="part-c-claims", title="grounding claims", rng=rng, head=head
    )

    # (b) pairs: G0a vs G0b, G0a vs G1, G1 vs G2 per target; shuffled, sides random.
    pairs = [
        (rec, kind, x, rec["cards"][x], y, rec["cards"][y])
        for rec in records
        for kind, x, y in (
            ("g0-g0", "g0a", "g0b"),
            ("g0-g1", "g0a", "g1"),
            ("g1-g2", "g1", "g2"),
        )
    ]
    n_pairs = _write_pairs_sheet(
        pairs, prefix="part-c-pairs", title="grounding pairs", rng=rng, head=head
    )
    for sheet, count in sheet_paths:
        print(f"wrote {sheet.relative_to(REPO)} ({count} units)")
    print(f"wrote plans/62-eval/part-c-pairs-sheet.md ({n_pairs} pairs)")


def _write_pairs_sheet(
    pairs: list[tuple[dict, str, str, dict, str, dict]],
    *,
    prefix: str,
    title: str,
    rng: random.Random,
    head: str,
    evidence: str = "evidence",
    repo: str = BIOME_REPO,
) -> int:
    """A blind pairs sheet and its key: ``(record, kind, label_x, card_x, label_y, card_y)``.

    Sides are randomized and the pairs shuffled; the pair kind and the labels
    are in ``<prefix>-key.json`` only. Returns the number of pairs.
    """
    placed = []
    for rec, kind, x, cx, y, cy in pairs:
        if rng.random() < 0.5:
            placed.append((rec, kind, x, cx, y, cy))
        else:
            placed.append((rec, kind, y, cy, x, cx))
    rng.shuffle(placed)
    pkey: dict[str, dict] = {}
    body = [
        f"# Plan 62 Part C - {title} sheet (judge)",
        "",
        f"For each of the **{len(placed)}** pairs: two cards of the same code. "
        "Question: **which card better explains why this code is the way it is, "
        "with fewer wrong or irrelevant statements?** Write `A`, `B` or `=` and a "
        "short reason. Pairs are shuffled; some pairs compare two cards made the "
        "same way. Check claims against today's code (shown) and the evidence file "
        f"named under each pair; repository: {repo}, HEAD `{head}` (read-only).",
        "",
        f"Do not open `{prefix}-key.json`, any `*-key*.json`, `part-c-cards*/` "
        "or `part-c-work/` other than the evidence files. Write your verdicts to "
        f"`plans/62-eval/{prefix}-judge.md` as one markdown table, "
        "`| Pair | Verdict | Reason |`, every pair, in sheet order.",
        "",
    ]
    for n, (rec, kind, a, ca, b, cb) in enumerate(placed, start=1):
        pid = f"P{n:02d}"
        pkey[pid] = {"tid": rec["tid"], "pair": kind, "A": a, "B": b}
        tgt = rec["target"]
        body += [f"## {pid} - `{tgt['qualified_name']}`", ""]
        body += _code_block(rec)
        body += [
            f"Evidence: `plans/62-eval/part-c-work/{evidence}/{rec['tid']}.txt`",
            "",
            "### Card A",
            "",
        ]
        body += _render_card(ca)
        body += ["### Card B", ""]
        body += _render_card(cb)
        body += ["**Verdict:** ", "", "---", ""]
    (EVAL_DIR / f"{prefix}-sheet.md").write_text("\n".join(body))
    (EVAL_DIR / f"{prefix}-key.json").write_text(json.dumps(pkey, indent=1))
    return len(placed)


def cmd_level_sheets(env: Env, args: argparse.Namespace) -> None:
    """Blind sheets for the level arms: ``low`` twice against ``medium`` (no LLM).

    ``low-a`` / ``low-b`` are the ``g2`` / ``g2b`` cards of
    ``grounding --level low``; ``medium`` is the grounding run's G2 card. The
    level is the hidden label. Claims: every unit of the three cards. Pairs:
    low-a vs low-b (the noise control) and low-a vs medium. The evidence file
    is the ``medium`` history (``part-c-work/evidence``, G1's prompt: no source
    block), which holds every PR the ``low`` bundle shows.
    """
    rng = random.Random(args.seed + 1)
    entries = []
    pairs = []
    for path in sorted(C_CARDS.glob("T*.json")):
        medium = json.loads(path.read_text())
        low_path = _cards_dir("low") / path.name
        if not low_path.exists():
            continue
        low = json.loads(low_path.read_text())
        cards = {
            "low-a": low.get("cards", {}).get("g2"),
            "low-b": low.get("cards", {}).get("g2b"),
            "medium": medium.get("cards", {}).get("g2"),
        }
        if any(c is None for c in cards.values()):
            continue
        entries.append((medium, {k: (c, {}) for k, c in cards.items()}))
        pairs.append(
            (medium, "low-low", "low-a", cards["low-a"], "low-b", cards["low-b"])
        )
        pairs.append(
            (medium, "low-medium", "low-a", cards["low-a"], "medium", cards["medium"])
        )
    if not entries:
        raise SystemExit("no target has both low cards and a medium card")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_biome_root(env),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    for sheet, count in _write_claims_sheets(
        entries,
        prefix="part-c-levels-claims",
        title="level claims",
        rng=rng,
        head=head,
    ):
        print(f"wrote {sheet.relative_to(REPO)} ({count} units)")
    n = _write_pairs_sheet(
        pairs, prefix="part-c-levels-pairs", title="level pairs", rng=rng, head=head
    )
    print(f"wrote plans/62-eval/part-c-levels-pairs-sheet.md ({n} pairs)")


def record_checks(rec: dict, arm: str) -> dict:
    """The verification verdicts of one arm's card, by claim id (``C1``, ``R1``)."""
    return (rec.get("verify", {}).get(arm) or {}).get("verdicts") or {}


def _biome_root(env: Env) -> Path:
    if env.root is None:
        _project_merged(env)
    assert env.root is not None
    return env.root


def cmd_grounding_summary(env: Env, args: argparse.Namespace) -> None:
    """Spend per arm, prompt sizes, ``coverage.source`` and verdict counts (no LLM)."""
    records = [json.loads(p.read_text()) for p in sorted(C_CARDS.glob("T*.json"))]
    spend: Counter = Counter()
    for line in LEDGER.read_text().splitlines():
        if line.strip():
            e = json.loads(line)
            if str(e.get("step", "")).startswith("plan62-part-c"):
                spend[e.get("arm", "-")] += float(e.get("cost") or 0.0)
    total = Ledger(LEDGER, GROUNDING_CAP_USD).total()
    out: dict[str, Any] = {
        "ledger_total": round(total, 4),
        "spend_by_arm": {k: round(v, 4) for k, v in sorted(spend.items())},
        "targets": {},
    }
    sources: Counter = Counter()
    verdicts: Counter = Counter()
    deltas = []
    for rec in records:
        pc = rec.get("prompt_chars", {})
        src = (rec.get("coverage", {}).get("g2") or {}).get("source")
        sources[src] += 1
        delta = pc.get("g2", 0) - pc.get("g1", 0)
        deltas.append(delta)
        checks = record_checks(rec, "g2")
        verdicts.update(v["verdict"] for v in checks.values())
        out["targets"][rec["tid"]] = {
            "code_chars": rec.get("code_chars"),
            "prompt_chars": pc,
            "g1_minus_g0": pc.get("g1", 0) - pc.get("g0a", 0),
            "g2_minus_g1": delta,
            "source": src,
            "bundle_commits": rec.get("bundle_commits"),
            "descriptions_waiting": len(rec.get("descriptions_waiting", [])),
            "checks_failed": rec.get("checks_failed"),
            "cards": sorted(rec.get("cards", {})),
            "items": {
                a: _n_items(c) + len(_sentences(c["why"]))
                for a, c in rec.get("cards", {}).items()
            },
            "tokens": {
                a: [c.get("input_tokens"), c.get("output_tokens")]
                for a, c in rec.get("cards", {}).items()
            },
            "verify_g2": dict(Counter(v["verdict"] for v in checks.values())),
            "errors": rec.get("errors", []),
        }
    out["source_counts"] = dict(sources)
    out["g2_minus_g1"] = {
        "median": statistics.median(deltas) if deltas else None,
        "max": max(deltas) if deltas else None,
        "sum": sum(deltas),
    }
    out["verify_g2_counts"] = dict(verdicts)
    C_WORK.mkdir(parents=True, exist_ok=True)
    path = C_WORK / "grounding-summary.json"
    path.write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v for k, v in out.items() if k != "targets"}, indent=1))
    print(f"wrote {path.relative_to(REPO)}")


# ---------------------------------------------------------------------------
# External corpus (a second repo for the level arms, e.g. axios)
# ---------------------------------------------------------------------------

EXT_PREFIX = "lib/"
"""Only symbols under this path are targets (the library, not tests or docs)."""
EXT_MIN_SPAN = 3


def _ext_dir(name: str) -> Path:
    return C_WORK / name


def _ext_cards_dir(name: str, level: str) -> Path:
    return EVAL_DIR / f"part-c-cards-{name}-{level}"


def _ext_source(name: str) -> dict:
    path = _ext_dir(name) / "source.json"
    if not path.exists():
        raise SystemExit(f"no {path}: run ext-copy --name {name} first")
    return json.loads(path.read_text())


def _ext_context(env: Env, name: str) -> Any:
    """The eval context on an external corpus's DB copies and its checkout (read-only)."""
    d = _ext_dir(name)
    return eval_context(
        env,
        d / "whygraph.db",
        d / "codegraph.db",
        root=Path(_ext_source(name)["root"]),
        slug=name,
    )


def cmd_ext_copy(env: Env, args: argparse.Namespace) -> None:
    """Copy a repo's WhyGraph and CodeGraph DBs (backup API) and migrate the copy (no LLM).

    The sources must not be held by a running container; the copies live in
    ``part-c-work/<name>/`` and every later command reads only them.
    """
    from whygraph.core.config import Config
    from whygraph.core.context import ProjectContext, use_project
    from whygraph.db.bootstrap import ensure_initialized

    root = Path(args.root).expanduser().resolve()
    wdb = (
        Path(args.wdb).expanduser() if args.wdb else root / ".whygraph" / "whygraph.db"
    )
    cdb = (
        Path(args.cdb).expanduser()
        if args.cdb
        else root / ".codegraph" / "codegraph.db"
    )
    d = _ext_dir(args.name)
    _sqlite_copy(wdb, d / "whygraph.db")
    _sqlite_copy(cdb, d / "codegraph.db")
    config = Config(whygraph_db=d / "whygraph.db")
    with use_project(ProjectContext(slug=args.name, root=root, config=config)):
        ensure_initialized()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    source = {
        "root": str(root),
        "head": head,
        "whygraph_db_from": str(wdb),
        "codegraph_db_from": str(cdb),
        "copied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (d / "source.json").write_text(json.dumps(source, indent=1))
    print(json.dumps(source, indent=1))


def _needs_description(item: Any, target_path: str, threshold: int) -> bool:
    """Whether the backfill would make an LLM call for ``item`` (see its two regimes)."""
    from whygraph.mcp.evidence import _cached_file_description

    commit = item.commit
    if commit.files_changed > threshold:
        return _cached_file_description(commit.sha, target_path) is None
    return commit.llm_description is None


def _describe_estimate(items: list, target_path: str, config: Any) -> tuple[int, float]:
    """Missing descriptions among ``items`` and their price, by the product's arithmetic.

    A normal commit is sized from its ``commit_file_change`` rows; a bulk
    commit is described for the target's file only, so it is sized as that
    one file (:func:`whygraph.portal.estimate.estimate_tokens`).
    """
    from sqlmodel import select

    from whygraph.db import get_session
    from whygraph.db.models import CommitFileChange
    from whygraph.portal.estimate import CommitSize, estimate_tokens, model_profile
    from whygraph.portal.prices import NO_OVERRIDES, price_for

    threshold = config.analyze.large_commit_file_count
    sizes = []
    for item in items:
        if not _needs_description(item, target_path, threshold):
            continue
        commit = item.commit
        with get_session() as session:
            rows = session.exec(
                select(CommitFileChange).where(
                    CommitFileChange.commit_sha == commit.sha
                )
            ).all()
            lines = {
                r.path: (r.lines_added or 0) + (r.lines_deleted or 0) for r in rows
            }
        if commit.files_changed > threshold:
            sizes.append(CommitSize(1, (lines.get(target_path, 20),)))
        else:
            per_file = tuple(lines.values()) or (20,)
            sizes.append(CommitSize(max(len(per_file), 1), per_file))
    if not sizes:
        return 0, 0.0
    est = estimate_tokens(
        sizes,
        max_diff_chars=config.analyze.max_diff_chars,
        large_commit_file_count=threshold,
        profile=model_profile(MODEL),
    )
    hit = price_for(PROVIDER, MODEL, NO_OVERRIDES, custom_endpoint=False)
    assert hit is not None
    price, _ = hit
    usd = (
        est.input_tokens * float(price.input) + est.output_tokens * float(price.output)
    ) / 1e6
    return len(sizes), usd


def _median_description_chars() -> int:
    from sqlmodel import col, func, select

    from whygraph.db import get_session
    from whygraph.db.models import Commit

    with get_session() as session:
        lengths = session.exec(
            select(func.length(Commit.llm_description))
            .where(col(Commit.llm_description).is_not(None))
            .limit(500)
        ).all()
    return int(statistics.median([n for n in lengths if n] or [1200]))


def cmd_ext_select(env: Env, args: argparse.Namespace) -> None:
    """Seeded symbol targets where ``low`` and ``medium`` differ, with a cost estimate (no LLM).

    Walks the library's functions and methods (unique qualified names, 3 to
    :data:`MAX_SPAN` lines, under :data:`EXT_PREFIX`) in a seeded order and,
    for each, builds G2's ``low`` and ``medium`` draft prompts with no call.
    It skips a candidate with no evidence, one whose two prompts are
    identical (counted), and one whose estimate (two ``low`` cards, one
    ``medium`` card, and the descriptions ``medium``'s bundle still needs,
    priced by the product's arithmetic) is over ``--max-target-usd``, until
    ``--n`` targets are taken. Writes ``part-c-work/<name>/targets.json`` and
    ``select.json``.
    """
    from whygraph.core import get_config
    from whygraph.core.context import use_project

    name = args.name
    d = _ext_dir(name)
    ctx = _ext_context(env, name)
    price_in, price_out = _price_per_m()
    con = sqlite3.connect(f"file:{d / 'codegraph.db'}?mode=ro", uri=True)
    rows = con.execute(
        "select id, kind, qualified_name, file_path, start_line, end_line from nodes "
        "where kind in ('function', 'method')"
    ).fetchall()
    con.close()
    names = Counter(r[2] for r in rows)
    candidates = sorted(
        (
            Node(*r)
            for r in rows
            if r[3].startswith(EXT_PREFIX)
            and names[r[2]] == 1
            and EXT_MIN_SPAN <= r[5] - r[4] + 1 <= MAX_SPAN
        ),
        key=lambda n: (n.path, n.start),
    )
    random.Random(args.seed).shuffle(candidates)
    old_prompt = _pre_r48_prompt()
    taken: list[dict] = []
    log: list[dict] = []
    skipped = Counter()
    with use_project(ctx):
        config = get_config()
        median_desc = _median_description_chars()
        for node in candidates:
            if len(taken) >= args.n:
                break
            row = {k: getattr(node, k) for k in Node.__annotations__}
            row["tid"] = f"A{len(taken) + 1:02d}"
            entry: dict[str, Any] = {"symbol": node.qualified_name, "path": node.path}
            evidence, kwargs, fields = _target_inputs(row, config, "low")
            if not evidence:
                skipped["no_evidence"] += 1
                log.append({**entry, "skip": "no_evidence"})
                continue
            prompts: dict[str, dict] = {}
            bundles = {}
            for level in ("low", "medium"):
                ev, kw, _ = _target_inputs(row, config, level)
                client = PromptOnlyClient()
                _arm_generator("g2", client, config, old_prompt).generate(ev, **kw)
                assert client.last_prompt is not None
                prompts[level] = client.last_prompt
                bundles[level] = kw["bundle"]
            chars = {k: sum(len(v) for v in p.values()) for k, p in prompts.items()}
            entry.update(chars=chars, commits=len(evidence))
            if prompts["low"] == prompts["medium"]:
                skipped["identical"] += 1
                log.append({**entry, "skip": "identical"})
                continue
            missing, describe_usd = _describe_estimate(
                list(bundles["medium"].items), node.path, config
            )
            missing_low, _ = _describe_estimate(
                list(bundles["low"].items), node.path, config
            )
            card_usd = (
                (
                    2 * (chars["low"] + missing_low * median_desc)
                    + chars["medium"]
                    + missing * median_desc
                )
                / CHARS_PER_TOKEN
                * price_in
                + 3 * EST_OUT_TOKENS * price_out
            ) / 1e6
            entry.update(
                missing_descriptions=missing,
                describe_usd=round(describe_usd, 4),
                cards_usd=round(card_usd, 4),
                total_usd=round(describe_usd + card_usd, 4),
            )
            if describe_usd + card_usd > args.max_target_usd:
                skipped["over_cost"] += 1
                log.append({**entry, "skip": "over_cost"})
                continue
            taken.append(row)
            log.append({**entry, "tid": row["tid"]})
            print(f"{row['tid']} {node.qualified_name} {entry}")
    total = sum(e.get("total_usd", 0.0) for e in log if "tid" in e)
    summary = {
        "seed": args.seed,
        "candidates": len(candidates),
        "examined": len(log),
        "taken": len(taken),
        "skipped": dict(skipped),
        "estimate_usd": round(total, 4),
        "describe_usd": round(sum(e["describe_usd"] for e in log if "tid" in e), 4),
        "missing_descriptions": sum(
            e["missing_descriptions"] for e in log if "tid" in e
        ),
        "max_target_usd": args.max_target_usd,
        "log": log,
    }
    (d / "targets.json").write_text(json.dumps(taken, indent=1))
    (d / "select.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k != "log"}, indent=1))


def cmd_ext_describe(env: Env, args: argparse.Namespace) -> None:
    """Describe what the targets' ``medium`` bundles still lack, before any card (paid).

    One backfill per target over the ``medium`` bundle (a superset of
    ``low``'s), with the cap checked before every describe call; every arm
    then reads the same stored descriptions and describes nothing itself.
    """
    global STEP
    STEP = f"plan62-part-c-{args.name}-describe"
    from whygraph.analyze import card_bundle_for
    from whygraph.core import get_config
    from whygraph.core.context import use_project
    from whygraph.core.usage import use_usage_sink
    from whygraph.mcp.evidence import backfill_evidence_descriptions, collect_evidence

    targets = json.loads((_ext_dir(args.name) / "targets.json").read_text())
    ledger = Ledger(LEDGER, args.cap, reserve=0.0)
    ctx = _ext_context(env, args.name)
    sink = EvalSink(ledger, ctx.config, None)
    per_call = 0.03  # a conservative describe call on Sonnet, for the cap check

    def allow() -> bool:
        if not args.yes:
            return False
        try:
            ledger.check(per_call)
        except BudgetStop:
            return False
        return True

    print(f"ledger ${ledger.total():.4f}; cap ${args.cap:.2f}")
    with use_project(ctx), use_usage_sink(sink):
        config = get_config()
        for row in targets:
            target = _target_of(Node(**{k: row[k] for k in Node.__annotations__}))
            evidence = collect_evidence(target, limit=20)
            items = list(card_bundle_for(evidence, config.rationale, "medium").items)
            missing, usd = _describe_estimate(items, target.path, config)
            if not missing:
                continue
            _CURRENT_CALL.set((row["tid"], "describe"))
            outcome = backfill_evidence_descriptions(
                items,
                target_path=target.path,
                allow=allow,
                max_commits=len(items),
                wall_sec=900,
            )
            print(
                f"{row['tid']} missing {missing} (~${usd:.4f}); described "
                f"{outcome.described}, waiting {len(outcome.waiting)}; "
                f"ledger ${ledger.total():.4f}"
            )
    print(f"ledger total ${ledger.total():.4f} of ${args.cap:.2f}")


def cmd_ext_sheets(env: Env, args: argparse.Namespace) -> None:
    """Blind level sheets for an external corpus: low-a, low-b and medium (no LLM)."""
    name = args.name
    src = _ext_source(name)
    rng = random.Random(args.seed + 2)
    evidence_rel = f"{name}/evidence"
    evidence_dir = C_WORK / evidence_rel
    evidence_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    pairs = []
    for path in sorted(_ext_cards_dir(name, "medium").glob("A*.json")):
        medium = json.loads(path.read_text())
        low_path = _ext_cards_dir(name, "low") / path.name
        if not low_path.exists():
            continue
        low = json.loads(low_path.read_text())
        cards = {
            "low-a": low.get("cards", {}).get("g2"),
            "low-b": low.get("cards", {}).get("g2b"),
            "medium": medium.get("cards", {}).get("g2"),
        }
        if any(c is None for c in cards.values()):
            continue
        # The medium history (a superset of low's) without the source block.
        (evidence_dir / f"{medium['tid']}.txt").write_text(
            _SOURCE_BLOCK_RE.sub("", medium["prompts"]["g2"]["user"], count=1)
        )
        entries.append((medium, {k: (c, {}) for k, c in cards.items()}))
        pairs.append(
            (medium, "low-low", "low-a", cards["low-a"], "low-b", cards["low-b"])
        )
        pairs.append(
            (medium, "low-medium", "low-a", cards["low-a"], "medium", cards["medium"])
        )
    if not entries:
        raise SystemExit("no target has both low cards and a medium card")
    repo = f"{name} at `{src['root']}`"
    for sheet, count in _write_claims_sheets(
        entries,
        prefix=f"part-c-{name}-levels-claims",
        title=f"{name} level claims",
        rng=rng,
        head=src["head"],
        evidence=evidence_rel,
        repo=repo,
    ):
        print(f"wrote {sheet.relative_to(REPO)} ({count} units)")
    n = _write_pairs_sheet(
        pairs,
        prefix=f"part-c-{name}-levels-pairs",
        title=f"{name} level pairs",
        rng=rng,
        head=src["head"],
        evidence=evidence_rel,
        repo=repo,
    )
    print(f"wrote plans/62-eval/part-c-{name}-levels-pairs-sheet.md ({n} pairs)")


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
    global MODEL
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--slug", default="biome")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument(
        "--db-url", default=os.environ.get("WHYGRAPH_DATABASE_URL", DEFAULT_DB_URL)
    )
    parser.add_argument("--seed", type=int, default=62)
    parser.add_argument(
        "--model", default=MODEL, help=f"{PROVIDER} model (default {MODEL})"
    )
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
    p.add_argument(
        "--arms",
        default=",".join(ARMS),
        help="comma-separated arms; a name containing treated gets the thread inputs",
    )
    p.add_argument(
        "--classes", help="comma-separated corpus classes to run (default all)"
    )
    p.add_argument(
        "--no-describe",
        action="store_true",
        help="describe nothing new (the cached descriptions still apply)",
    )
    sub.add_parser("prompts")
    sub.add_parser("m1")
    sub.add_parser("measure")
    sub.add_parser("repeats")
    p = sub.add_parser("rerun")
    p.add_argument("--prefix", default="sonnet-", help="arm-name prefix of the run")
    p.add_argument("--suffix", default="sonnet", help="suffix of the sheet files")
    p.add_argument("--title", default="Sonnet re-run", help="report section heading")
    p = sub.add_parser("grounding")
    p.add_argument("--yes", action="store_true", help="spend (default: estimate)")
    p.add_argument("--arms", help="default: g0a,g0b,g1,g2 at medium; g2,g2b at low")
    p.add_argument("--cap", type=float, default=GROUNDING_CAP_USD)
    p.add_argument("--no-portal-ledger", action="store_true")
    p.add_argument(
        "--level",
        default=GROUNDING_LEVEL,
        choices=LEVELS_OFFERED,
        help="medium: the grounding arms; low: the level arms",
    )
    p.add_argument(
        "--corpus",
        default="biome",
        help="biome (the thread targets) or an ext-copy name (its targets.json)",
    )
    p = sub.add_parser("ext-copy")
    p.add_argument("--name", required=True)
    p.add_argument("--root", required=True, help="the repo checkout (read-only)")
    p.add_argument("--wdb", help="WhyGraph DB to copy (default <root>/.whygraph)")
    p.add_argument("--cdb", help="CodeGraph DB to copy (default <root>/.codegraph)")
    p = sub.add_parser("ext-select")
    p.add_argument("--name", required=True)
    p.add_argument("--n", type=int, default=15)
    p.add_argument("--max-target-usd", type=float, default=0.25)
    p = sub.add_parser("ext-describe")
    p.add_argument("--name", required=True)
    p.add_argument("--yes", action="store_true", help="spend (default: estimate)")
    p.add_argument("--cap", type=float, default=25.0)
    p = sub.add_parser("ext-sheets")
    p.add_argument("--name", required=True)
    sub.add_parser("grounding-sheets")
    sub.add_parser("level-sheets")
    sub.add_parser("grounding-summary")
    sub.add_parser("level-prompts")
    args = parser.parse_args(argv)
    MODEL = args.model
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
        "repeats": cmd_repeats,
        "rerun": cmd_rerun,
        "grounding": cmd_grounding,
        "grounding-sheets": cmd_grounding_sheets,
        "grounding-summary": cmd_grounding_summary,
        "level-prompts": cmd_level_prompts,
        "level-sheets": cmd_level_sheets,
        "ext-copy": cmd_ext_copy,
        "ext-select": cmd_ext_select,
        "ext-describe": cmd_ext_describe,
        "ext-sheets": cmd_ext_sheets,
    }
    commands[args.cmd](env, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
