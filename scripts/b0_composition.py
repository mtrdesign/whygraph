"""Plan 62 gate B0: measure prompt composition per depth level and propose the stoppers.

Read-only and offline (no LLM call, no scan, no portal). It models the CURRENT
plan rules (``plans/62-rationale-layer-plan.md`` sections 13.2 and 13.3):

* Ring 0 holds every line-level unit; a squash commit and its ``pr-origin``
  commits are one unit dated by the squash commit.
* Full PRs are the own PRs of Ring 0 units (``low``: of ``blame`` units;
  ``medium`` and above: of all units), filled in Ring 0 order and stopped at
  the FIRST PR that does not fit the count ceiling or the character budget.
* PRs that contain a commit only through their roster are one
  ``also in N other PRs`` line; merge / squash bodies are never rendered.
* Compact context commits per level (low 0, medium 4, high 6); feature-branch
  commits of squash units from ``high``.

Three sub-commands::

    migrate --wdb COPY.db                # bring a COPY of a project DB to head
    collect --repo NAME --root R --wdb COPY.db [--cdb COPY.db] --out facts.json
    report  --facts a.json b.json ... --out plans/62-eval/b0-composition.md

``collect`` is the slow part (git blame per target); ``report`` replays the
rules over the stored facts, so the constants can be re-derived in seconds.
Always pass COPIES of the databases; ``collect`` refuses a database inside the
dev portal's data directory.

Run with ``LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 uv run --no-sync python
scripts/b0_composition.py ...``.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta
from collections import Counter
from pathlib import Path

CPT = 2.46  # chars per token, calibrated on cached cards (plan 62 section 13.1)
SYS_CHARS = 3035 + 87  # rationale_generator system + task prompt
HEADER_CHARS = 200
LINE_SOURCES = frozenset({"blame", "pr-origin", "blame-walked", "predecessor-blame"})
SOURCE_LABELS = {
    "blame": "line-blame",
    "pr-origin": (
        "draft commit inside a squash-merged PR "
        "(intermediate states never reached the default branch)"
    ),
    "blame-walked": "line-blame (skipped a refactor commit)",
    "predecessor-blame": "line-blame on a pre-rename predecessor file",
    "area": "area-history (touched the file but not these lines)",
}
COLLECT_LIMIT = 20
LEVELS = ("low", "medium", "high")
# level -> (rule over units, context commits, feature-branch commits of squash units)
LEVEL_SPEC = {
    "low": ("blame", 0, False),
    "medium": ("all", 4, False),
    "high": ("all", 6, True),
}
BUDGET_CANDIDATES = (10_000, 15_000, 20_000, 30_000, 40_000)
EXTRA_CANDIDATES = (50_000, 60_000, 80_000)  # informational, outside the rule's list
# Stored descriptions measured 0.60x the estimate_tokens output * 3.5 chars
# (n = 283 described axios commits, mean ratio 0.604, median 0.532).
DESC_CHARS_PER_OUT_TOKEN = 3.5 * 0.6
MAX_CUT_SHARE = 0.10
SEPARATION = 0.15
OUT_TOKENS = 1500  # typical card output, for the price column
RELEASE_RE = re.compile(
    r"(?i)\b(release|sync|back-?merge|promote)\b|^v?\d+\.\d+(\.\d+)?|^merge (branch|pull)"
    r"|^chore\(release\)|\bbump\b.*\bversion\b|\bversion bump\b"
)
STRATA = (("<5", 0, 4, 15), ("5-19", 5, 19, 35), (">=20", 20, 10**9, 50))
DEV_DIR_MARK = "/whygraph-dev/"


# --------------------------------------------------------------------------- utils


def pct(xs: list[float], p: float) -> float:
    """Nearest-rank percentile used by the seed scripts.

    Parameters
    ----------
    xs : list of float
        Values (any order, may be empty).
    p : float
        Fraction in ``[0, 1]``.

    Returns
    -------
    float
        The value at rank ``int(p * n)``; ``0`` for an empty list.
    """
    if not xs:
        return 0
    ys = sorted(xs)
    return ys[min(len(ys) - 1, int(p * len(ys)))]


def _ro(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite file read-only."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _refuse_live(path: str | Path | None) -> None:
    """Exit when ``path`` sits in the dev portal's data directory."""
    if path and DEV_DIR_MARK in str(path) and "/scratchpad/" not in str(path):
        raise SystemExit(f"refusing {path}: pass a COPY of the database")


def _git(root: str, *args: str) -> str:
    """Run a read-only git command in ``root`` and return stdout ('' on failure)."""
    proc = subprocess.run(
        ["git", "-C", root, "-c", "core.quotepath=off", *args],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else ""


# --------------------------------------------------------------------------- migrate


def cmd_migrate(args: argparse.Namespace) -> None:
    """Bring a COPY of a project database up to the current schema head."""
    from whygraph.core.config import Config
    from whygraph.core.context import ProjectContext, use_project
    from whygraph.db.bootstrap import ensure_initialized

    _refuse_live(args.wdb)
    cfg = Config(whygraph_db=Path(args.wdb))
    with use_project(ProjectContext(slug="b0", root=Path(args.wdb).parent, config=cfg)):
        print("migrated", ensure_initialized())


# --------------------------------------------------------------------------- targets


def _history_lengths(wcon: sqlite3.Connection) -> dict[str, int]:
    """Distinct commits per path from ``commit_file_change``."""
    rows = wcon.execute(
        "select path, count(distinct commit_sha) from commit_file_change group by path"
    ).fetchall()
    return {p: n for p, n in rows}


def _stratum(n: int) -> str:
    """Name of the file-history stratum for ``n`` commits."""
    for name, lo, hi, _ in STRATA:
        if lo <= n <= hi:
            return name
    return STRATA[-1][0]


def _fill_quota(by_stratum: dict[str, list], total: int) -> list:
    """Take the per-stratum quotas (weighted to long histories); spill shortfalls long-first."""
    scale = total / sum(q for *_, q in STRATA)
    picked: list = []
    spare: list = []
    for name, _, _, q in STRATA:
        pool = by_stratum.get(name, [])
        take = round(q * scale)
        picked += pool[:take]
        spare += [(name, x) for x in pool[take:]]
    order = {name: i for i, (name, *_) in enumerate(reversed(STRATA))}
    spare.sort(key=lambda t: order[t[0]])
    for _, x in spare:
        if len(picked) >= total:
            break
        picked.append(x)
    return picked[:total]


def _cg_targets(root: Path, wcon, cdb: str, n: int, rng: random.Random) -> list[dict]:
    """Function / method targets from a CodeGraph copy, stratified by file history."""
    hist = _history_lengths(wcon)
    con = _ro(cdb)
    rows = con.execute(
        "select id, qualified_name, file_path, start_line, end_line from nodes "
        "where kind in ('function','method') and end_line - start_line >= 2 "
        "and file_path not like '%test%' and file_path not like '%spec%'"
    ).fetchall()
    callers = Counter()
    for tgt, cnt in con.execute(
        "select target, count(distinct source) from edges where kind='calls' group by target"
    ):
        callers[tgt] = cnt
    rng.shuffle(rows)
    existing: dict[str, int] = {}
    cands = []
    for nid, qn, fp, s, e in rows:
        if fp not in existing:
            f = root / fp
            existing[fp] = (
                sum(1 for _ in f.open(errors="replace")) if f.is_file() else -1
            )
        if existing[fp] < e:
            continue
        cands.append(
            dict(
                qn=qn,
                path=fp,
                start=s,
                end=e,
                hist=hist.get(fp, 0),
                callers=callers[nid],
            )
        )
    important = [c for c in cands if c["callers"] >= 5][:10]
    ids = {(c["path"], c["start"]) for c in important}
    by_stratum: dict[str, list] = {}
    for c in cands:
        if (c["path"], c["start"]) in ids:
            continue
        by_stratum.setdefault(_stratum(c["hist"]), []).append(c)
    sample = _fill_quota(by_stratum, n)
    for c in sample:
        c["important"] = False
    for c in important:
        c["important"] = True
    return sample + important


_RUST_FN = re.compile(
    r"^(\s*)(?:pub(?:\([^)]*\))?\s+)?(?:async\s+|const\s+|unsafe\s+)*fn\s+(\w+)"
)


def _rust_targets(root: Path, wcon, n: int, rng: random.Random) -> list[dict]:
    """Rust ``fn`` targets (regex, no CodeGraph) stratified by file history."""
    hist = _history_lengths(wcon)
    skip = ("test", "fixture", "bench", "spec", "snapshot", "generated", "/gen/")
    files = [
        p
        for p in hist
        if p.endswith(".rs") and "/src/" in p and not any(s in p for s in skip)
    ]
    rng.shuffle(files)
    by_stratum: dict[str, list] = {}
    quota = {name: round(q * n / 100) for name, _, _, q in STRATA}
    for p in files:
        st = _stratum(hist[p])
        if len(by_stratum.get(st, [])) >= quota[st] * 3:
            continue
        f = root / p
        if not f.is_file():
            continue
        lines = f.read_text(errors="replace").splitlines()
        fns = []
        for i, line in enumerate(lines):
            m = _RUST_FN.match(line)
            if not m:
                continue
            indent = m.group(1)
            for j in range(i + 1, min(len(lines), i + 400)):
                if (
                    lines[j].startswith(indent + "}")
                    and len(lines[j]) == len(indent) + 1
                ):
                    if j - i >= 2:
                        fns.append((m.group(2), i + 1, j + 1))
                    break
        if fns:
            qn, s, e = rng.choice(fns)
            by_stratum.setdefault(st, []).append(
                dict(
                    qn=qn,
                    path=p,
                    start=s,
                    end=e,
                    hist=hist[p],
                    callers=0,
                    important=False,
                )
            )
    return _fill_quota(by_stratum, n)


# --------------------------------------------------------------------------- facts


class _Describer:
    """Cold description size model (``portal/estimate.estimate_tokens``)."""

    def __init__(self, root: str, wcon: sqlite3.Connection) -> None:
        self.root = root
        self.wcon = wcon
        self.numstat: dict[str, list[tuple[str, int]]] = {}

    def _files(self, sha: str) -> list[tuple[str, int]]:
        rows = self.wcon.execute(
            "select path, lines_added + lines_deleted from commit_file_change where commit_sha=?",
            (sha,),
        ).fetchall()
        if rows:
            return rows
        if sha not in self.numstat:
            out = _git(self.root, "diff", "--numstat", f"{sha}^1", sha) or _git(
                self.root, "show", "--numstat", "--format=", sha
            )
            files = []
            for line in out.splitlines():
                a, d, *p = line.split("\t")
                files.append(
                    (
                        p[0] if p else "",
                        (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0),
                    )
                )
            self.numstat[sha] = files
        return self.numstat[sha]

    def tokens(self, sha: str, path: str) -> tuple[int, int]:
        """Return ``(input_tokens, output_tokens)`` to describe ``sha`` for ``path``."""
        from whygraph.portal.estimate import CommitSize, estimate_tokens

        fc = self._files(sha)
        if not fc:
            return 0, 0
        if len(fc) > 30:  # bulk commit: described per file slice
            fc = [r for r in fc if r[0] == path] or fc[:1]
        e = estimate_tokens(
            [CommitSize(len(fc), tuple(r[1] for r in fc))],
            max_diff_chars=50_000,
            large_commit_file_count=10**9,
        )
        return e.input_tokens, e.output_tokens


def _item_fact(item, rg, desc: _Describer, path: str) -> dict:
    """Size facts for one evidence item (commit, source, linked PRs)."""
    c = item.commit
    plain = c.model_copy(update={"body": "", "llm_description": None})
    base = (
        len("\n".join(rg._format_commit(plain)))
        + len(f"  Source: {SOURCE_LABELS.get(item.source, item.source)}")
        + 2
    )
    body = (c.body or "").strip()
    body_chars = len("  Body:\n" + rg._indent_block(body, "    ")) + 1 if body else 0
    d_in = d_out = 0
    if c.llm_description:
        desc_chars = len("  Summary: ") + len(c.llm_description) + 1
        missing = False
    else:
        d_in, d_out = desc.tokens(c.sha, path)
        desc_chars = int(d_out * 3.5) + 12
        missing = True
    return dict(
        sha=c.sha,
        src=item.source,
        date=str(c.committed_at or ""),
        author=c.author_name or "",
        subject=len(c.subject or ""),
        base=base,
        body=body_chars,
        desc=desc_chars,
        desc_missing=missing,
        d_in=d_in,
        d_out=d_out,
        prs=[p.number for p in item.pull_requests],
        pr_title=(item.pull_requests[0].title if item.pull_requests else ""),
    )


def _pr_fact(pr, rg, caps, issues_by_pr: dict[int, list[tuple[int, int]]]) -> dict:
    """Size facts for one pull request."""
    body = (pr.body or "").strip()
    roster = sum(
        len(c.get("headline", "")) + 20
        for c in rg._json_list(pr.commit_titles)[: caps.pr_roster_max_commits]
        if isinstance(c, dict)
    )
    comments = [
        c
        for c in rg._json_list(pr.comments)[: caps.pr_discussion_max_comments]
        if isinstance(c, dict)
    ]
    disc = sum(
        min(len((c.get("body") or "").strip()), caps.pr_comment_max_chars) + 20
        for c in comments
    )
    threads = rg._json_list(getattr(pr, "review_threads", "[]"))
    summaries = rg._json_list(getattr(pr, "review_summaries", "[]"))
    return dict(
        number=pr.number,
        title=pr.title or "",
        block=len("\n".join(rg._format_pr(pr, caps))) + 2,
        body=len(body),
        roster=roster,
        disc=disc,
        comments=len(comments),
        threads=len(threads) + len(summaries),
        merge_sha=pr.merge_commit_sha,
        head_sha=pr.head_sha,
        release=bool(RELEASE_RE.search(pr.title or "")),
        issues=issues_by_pr.get(pr.number, []),
    )


def _apply_origin_guarantee(line: list, limit: int = COLLECT_LIMIT) -> list:
    """Section 13.4: all ``blame`` items, the oldest dated one, then newest, up to ``limit``."""
    if len(line) <= limit:
        return line
    keep = [i for i in line if i.source == "blame"][:limit]
    dated = [i for i in line if i.commit.committed_at]
    if dated and dated[-1] not in keep:
        keep.append(dated[-1])
    for i in line:
        if len(keep) >= limit:
            break
        if i not in keep:
            keep.append(i)
    return [i for i in line if i in keep]


def _replay_window(
    root: str, target: dict, line_shas: set[str], head_date: str
) -> dict:
    """Replay the last 90 days of file commits: Ring 0 miss, line shift, elsewhere.

    Lines are tracked by their HEAD position (an approximation: a commit whose
    lines were overwritten again inside the window counts as 'elsewhere').
    """
    out = _git(
        root,
        "log",
        "--since",
        (datetime.fromisoformat(head_date) - timedelta(days=90)).isoformat(),
        "--format=%H",
        "--",
        target["path"],
    ).split()[:40]
    touch = shift = elsewhere = 0
    for sha in out:
        if sha in line_shas:
            touch += 1
            continue
        hunks = re.findall(
            r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@",
            _git(root, "show", "-U0", "--format=", sha, "--", target["path"]),
            re.M,
        )
        shifted = False
        for a, b, _c, d in hunks:
            old_len = int(b) if b != "" else 1
            new_len = int(d) if d != "" else 1
            if int(a) + old_len <= target["start"] and old_len != new_len:
                shifted = True
        if shifted:
            shift += 1
        else:
            elsewhere += 1
    return dict(commits=len(out), touch=touch, shift=shift, elsewhere=elsewhere)


def _facts_for(
    t: dict, root: Path, wcon, desc: _Describer, with_replay: bool
) -> dict | None:
    """Collect one target's size facts (needs the project context bound)."""
    from whygraph.analyze import rationale_generator as rg
    from whygraph.mcp.evidence import collect_evidence
    from whygraph.mcp.rationale import _symbol_context
    from whygraph.mcp.targets import Target

    target = Target(
        path=t["path"],
        line_start=t["start"],
        line_end=t["end"],
        qualified_name=t.get("qn"),
    )
    t0 = time.time()
    try:
        ev = collect_evidence(target, limit=40)
    except Exception as exc:  # noqa: BLE001 - a bad target is skipped, reported by the caller
        print("skip", t["qn"], str(exc)[:80], file=sys.stderr)
        return None
    if not ev:
        return None
    secs = time.time() - t0
    try:
        sc = _symbol_context(target)
        symctx = len(rg._format_symbol_context(sc)) if sc else 0
    except Exception:  # noqa: BLE001 - no CodeGraph copy for this repo
        symctx = 0
    caps = rg._PrRenderCaps()
    line_all = [i for i in ev if i.source in LINE_SOURCES]
    line = _apply_origin_guarantee(line_all)
    area = [i for i in ev if i.source not in LINE_SOURCES][
        : max(0, COLLECT_LIMIT - len(line))
    ]
    items = line + area
    pr_objs = {}
    for i in items:
        for p in i.pull_requests:
            pr_objs.setdefault(p.number, p)
    issue_chars = {}
    for i in items:
        for isu in i.issues:
            issue_chars[isu.number] = len("\n".join(rg._format_issue(isu)))
    links = {}
    nums = list(pr_objs)
    if nums:
        q = ",".join(str(n) for n in nums)
        for pr_n, is_n in wcon.execute(
            f"select pr_number, issue_number from pr_issue_link where link_kind='closes' and pr_number in ({q})"
        ):
            if is_n in issue_chars:
                links.setdefault(pr_n, []).append((is_n, issue_chars[is_n]))
    facts = dict(
        t,
        stratum=_stratum(t["hist"]),
        secs=secs,
        symctx=symctx,
        line_total=len(line_all),
        line_n=len(line),
        today_items=_today_chars(ev, rg, caps),
        items=[_item_fact(i, rg, desc, t["path"]) for i in items],
        prs={str(n): _pr_fact(p, rg, caps, links) for n, p in pr_objs.items()},
    )
    if with_replay:
        facts["replay"] = _replay_window(
            str(root),
            t,
            {i.commit.sha for i in line},
            _git(str(root), "log", "-1", "--format=%cI").strip(),
        )
    return facts


def _today_chars(ev: list, rg, caps) -> int:
    """Characters of today's bundle (every PR rendered per linked commit, bodies kept)."""
    first20 = [i for i in ev if i.source in LINE_SOURCES][:COLLECT_LIMIT]
    first20 += [i for i in ev if i.source not in LINE_SOURCES][
        : max(0, COLLECT_LIMIT - len(first20))
    ]
    return len(rg._format_evidence(first20, caps))


def cmd_collect(args: argparse.Namespace) -> None:
    """Sample targets of one repo and store their size facts as JSON."""
    from whygraph.core.config import Config
    from whygraph.core.context import ProjectContext, use_project

    _refuse_live(args.wdb)
    root = Path(args.root)
    wcon = _ro(args.wdb)
    rng = random.Random(62)
    if args.cdb:
        targets = _cg_targets(root, wcon, args.cdb, args.n, rng)
    else:
        targets = _rust_targets(root, wcon, args.n, rng)
    cfg = Config(
        whygraph_db=Path(args.wdb),
        **({"codegraph_db": Path(args.cdb)} if args.cdb else {}),
    )
    desc = _Describer(str(root), wcon)
    facts, cached = [], []
    with use_project(ProjectContext(slug="b0", root=root, config=cfg)):
        for k, t in enumerate(targets):
            f = _facts_for(t, root, wcon, desc, with_replay=True)
            if f:
                facts.append(f)
            if k % 10 == 0:
                print(args.repo, k, "/", len(targets), file=sys.stderr, flush=True)
        for row in wcon.execute(
            "select path, line_start, line_end, qualified_name, purpose, why, constraints, tradeoffs, "
            "risks, input_tokens, actual_model from rationale_cache"
        ).fetchall():
            path, s, e, qn, *texts, tokens, model = row
            t = dict(
                qn=qn,
                path=path,
                start=s,
                end=e,
                hist=_history_lengths(wcon).get(path, 0),
                callers=0,
                important=False,
            )
            f = _facts_for(t, root, wcon, desc, with_replay=False)
            if f:
                f.update(
                    card_text=" ".join(x or "" for x in texts),
                    card_tokens=tokens,
                    card_model=model,
                )
                cached.append(f)
    Path(args.out).write_text(
        json.dumps(
            dict(
                repo=args.repo,
                root=str(root),
                merge_based=args.merge_based,
                n_requested=args.n,
                targets=facts,
                cached_cards=cached,
                has_codegraph=bool(args.cdb),
            )
        )
    )
    print("wrote", args.out, len(facts), "targets", len(cached), "cached cards")


# --------------------------------------------------------------------------- simulation


def _units(facts: dict, expand: bool) -> list[dict]:
    """Group Ring 0 items into units (squash commit + its pr-origin commits).

    Returns
    -------
    list of dict
        Each unit has ``head`` (item), ``members`` (feature-branch items),
        ``blame`` (holds a ``blame`` item) and the Ring 0 ``order`` applied.
    """
    prs = facts["prs"]
    line = [i for i in facts["items"] if i["src"] in LINE_SOURCES]
    shas = {i["sha"] for i in line}

    def squash_parent(i):
        if i["src"] != "pr-origin":
            return None
        for n in i["prs"]:
            m = prs[str(n)]["merge_sha"]
            if m and m in shas and m != i["sha"]:
                return m
        return None

    parent = {i["sha"]: squash_parent(i) for i in line}
    units = {}
    for i in line:
        if parent[i["sha"]] is None:
            units[i["sha"]] = dict(head=i, members=[])
    for i in line:
        p = parent[i["sha"]]
        if p is not None:
            units[p]["members"].append(i)
    out = list(units.values())  # newest first (collection order)
    for u in out:
        u["blame"] = u["head"]["src"] == "blame" or any(
            m["src"] == "blame" for m in u["members"]
        )
    blame_units = [u for u in out if u["blame"]]
    dated = [u for u in out if u["head"]["date"]]
    origin = dated[-1] if dated else None
    order = list(blame_units)
    if origin is not None and origin not in order:
        order.append(origin)
    order += [u for u in out if u not in order]
    return order


def _desc(item: dict) -> int:
    """Rendered description characters: stored, or the calibrated estimate."""
    if item["desc_missing"]:
        return int(item["d_out"] * DESC_CHARS_PER_OUT_TOKEN) + 12
    return item["desc"]


def _own(item: dict, prs: dict) -> list[int]:
    """Own PRs of one item: its ``merge_commit_sha`` or ``head_sha`` is the item."""
    return [
        n
        for n in item["prs"]
        if item["sha"] in (prs[str(n)]["merge_sha"], prs[str(n)]["head_sha"])
    ]


def demand_prs(facts: dict, level: str) -> list[int]:
    """Own PRs the level's rule asks to show in full, in Ring 0 order."""
    rule, _, expand = LEVEL_SPEC[level]
    prs = facts["prs"]
    out: list[int] = []
    for u in _units(facts, expand):
        if rule == "blame" and not u["blame"]:
            continue
        items = [u["head"]] + (u["members"] if expand else [])
        for it in items:
            for n in _own(it, prs):
                if n not in out:
                    out.append(n)
    return out


def simulate(
    facts: dict,
    level: str,
    count_cap: int = 10**6,
    budget: int = 10**9,
    *,
    stoppers: bool = True,
    pr_once: bool = True,
    drop_body: bool = True,
    own_rule: bool = True,
    ctx_budget: int = 10**9,
    ring1: bool = True,
) -> dict:
    """Assemble one card's prompt size for ``level`` under the plan's rules.

    Parameters
    ----------
    facts : dict
        One target's facts from ``collect``.
    level : str
        ``low``, ``medium`` or ``high``.
    count_cap, budget : int
        Stopper 1: the full-PR count ceiling and character budget.
    stoppers, pr_once, drop_body, own_rule : bool
        Toggles for the ablation columns (all on = the plan's rules).
    ctx_budget : int
        ``[rationale].context_max_chars``.
    ring1 : bool
        Include context commits at all.

    Returns
    -------
    dict
        ``tokens``, ``chars``, a ``comp`` dict of characters per block kind and
        counters for what each stopper did.
    """
    rule, ccap, expand = LEVEL_SPEC[level]
    prs = facts["prs"]
    order = _units(facts, expand)
    demand = demand_prs(facts, level)
    if not own_rule:  # everything linked renders in full (no ownership rule)
        demand = []
        for u in order:
            for it in [u["head"]] + (u["members"] if expand else []):
                demand += [n for n in it["prs"] if n not in demand]
    full: list[int] = []
    used = 0
    stop = None
    for n in demand:
        blk = prs[str(n)]["block"]
        if stoppers and (len(full) >= count_cap or used + blk > budget):
            stop = "count" if len(full) >= count_cap else "budget"
            break
        full.append(n)
        used += blk
    demoted = [n for n in demand if n not in full]
    comp: Counter = Counter()
    comp["system"] = SYS_CHARS + HEADER_CHARS
    comp["symbol_context"] = facts["symctx"]
    seen: set[int] = set()
    issues: dict[int, int] = {}
    cold = Counter()
    undescribed = 0
    items = []
    for u in order:
        items.append(u["head"])
        if expand:
            items += u["members"]
    for it in items:
        merge_of = [n for n in it["prs"] if prs[str(n)]["merge_sha"] == it["sha"]]
        comp["commit_text"] += it["base"]
        if it["body"]:
            if merge_of and drop_body:
                comp["squash_body_placeholder"] += 55
            elif merge_of:
                comp["squash_body"] += it["body"]
            else:
                comp["commit_text"] += it["body"]
        comp["descriptions"] += _desc(it)
        if it["desc_missing"]:
            undescribed += 1
            cold["in"] += it["d_in"]
            cold["out"] += it["d_out"]
        roster_only = 0
        own = set(_own(it, prs))
        for n in it["prs"]:
            p = prs[str(n)]
            if n in full:
                if n in seen and pr_once:
                    comp["pr_shown_above"] += 30
                    continue
                seen.add(n)
                comp["pr_body"] += p["body"]
                comp["roster"] += p["roster"]
                comp["discussion"] += p["disc"]
                comp["pr_other"] += max(
                    0, p["block"] - p["body"] - p["roster"] - p["disc"]
                )
                for num, ch in p["issues"]:
                    issues.setdefault(num, ch)
            elif n in own or not own_rule:
                comp["pr_title_line"] += len(p["title"]) + 22
            else:
                roster_only += 1
        if roster_only:
            comp["roster_only_line"] += 30 + 9 * roster_only
    comp["issues"] = sum(issues.values())
    ctx_chars = []
    ctx_n = 0
    if ring1 and ccap:
        area = [i for i in facts["items"] if i["src"] not in LINE_SOURCES]
        used_ctx = 0
        for it in area[:ccap]:
            blk = (
                24
                + len(it["date"])
                + len(it["author"])
                + it["subject"]
                + _desc(it)
                + 20
                + (len(it["pr_title"]) + 14 if it["pr_title"] else 0)
            )
            if used_ctx + blk > ctx_budget:
                break
            used_ctx += blk
            ctx_chars.append(blk)
            ctx_n += 1
            if it["desc_missing"]:
                undescribed += 1
                cold["in"] += it["d_in"]
                cold["out"] += it["d_out"]
        comp["context"] = used_ctx
    chars = sum(comp.values())
    return dict(
        chars=chars,
        tokens=chars / CPT,
        comp=dict(comp),
        demand=len(demand),
        full=len(full),
        demoted=len(demoted),
        stop=stop,
        ctx_chars=ctx_chars,
        ctx_n=ctx_n,
        undescribed=undescribed,
        cold_in=cold["in"],
        cold_out=cold["out"],
        full_chars=used,
        pr_blocks=sum(len(i["prs"]) for i in items),
        pr_unique=len({n for i in items for n in i["prs"]}),
        demoted_nums=demoted,
    )


# --------------------------------------------------------------------------- prices


def load_prices() -> dict:
    """Sonnet 5.5 and the cheapest row of the bundled price table (USD per million)."""
    table = json.loads(
        (Path(__file__).parent.parent / "src/whygraph/portal/prices.json").read_text()
    )
    rows = table["rows"]
    sonnet = next(r for r in rows if r["model"] == "claude-sonnet-5-5")
    haiku = next(r for r in rows if r["model"] == "claude-haiku-5-5")
    cheapest = min(rows, key=lambda r: r["input"] * 20_000 + r["output"] * OUT_TOKENS)
    return dict(sonnet=sonnet, haiku=haiku, cheapest=cheapest, as_of=table["as_of"])


def card_price(tokens: float, row: dict) -> float:
    """USD for one card call: ``tokens`` in, :data:`OUT_TOKENS` out."""
    return (tokens * row["input"] + OUT_TOKENS * row["output"]) / 1e6


# --------------------------------------------------------------------------- report


def _all_targets(datasets: list[dict]) -> list[tuple[str, dict]]:
    return [(d["repo"], f) for d in datasets for f in d["targets"]]


def _dist(xs: list[float]) -> str:
    return f"{pct(xs, 0.5):.0f} / {pct(xs, 0.9):.0f} / {pct(xs, 0.99):.0f} / {max(xs, default=0):.0f}"


def _md_table(head: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def _round_up(x: float, step: int) -> int:
    return int(math.ceil(x / step) * step)


def derive_constants(datasets: list[dict]) -> dict:
    """Apply the decision rule (section 13.3 steps 1-3b) to the pooled sample.

    Returns
    -------
    dict
        ``demand`` per level (pooled and per repo), the budget candidate table,
        and the proposed ``count`` / ``budget`` per level before the quality
        check and the total cap.
    """
    targets = _all_targets(datasets)
    res: dict = {"levels": {}}
    prev_count = prev_budget = 0
    for lvl in LEVELS:
        dem = [len(demand_prs(f, lvl)) for _, f in targets]
        count = max(prev_count, int(pct(dem, 0.99)) or 1)
        cand = []
        for b in BUDGET_CANDIDATES + EXTRA_CANDIDATES:
            sims = [(r, simulate(f, lvl, count, b)) for r, f in targets]
            cut = sum(1 for _, s in sims if s["demoted"])
            cand.append(
                dict(
                    budget=b,
                    cut=cut / len(sims),
                    cut_n=cut,
                    p90=pct([s["tokens"] for _, s in sims], 0.9),
                    p99=pct([s["tokens"] for _, s in sims], 0.99),
                )
            )
        ok = [
            c
            for c in cand
            if c["cut"] <= MAX_CUT_SHARE and c["budget"] in BUDGET_CANDIDATES
        ]
        extra_ok = [c for c in cand if c["cut"] <= MAX_CUT_SHARE]
        chosen = ok[0]["budget"] if ok else None
        budget = max(prev_budget, chosen if chosen else BUDGET_CANDIDATES[-1])
        res["levels"][lvl] = dict(
            demand=dem,
            demand_by_repo={
                d["repo"]: [len(demand_prs(f, lvl)) for f in d["targets"]]
                for d in datasets
            },
            count=count,
            budget=budget,
            budget_found=bool(ok),
            extra_budget=extra_ok[0]["budget"] if extra_ok else None,
            candidates=cand,
        )
        prev_count, prev_budget = count, budget
    return res


def quality_check(datasets: list[dict], consts: dict) -> list[dict]:
    """Free check: would PRs a cached card mentions still render in full per level?

    The cached rows store items as plain strings (no refs), so ``PR #n`` /
    ``#n`` mentions in the card text stand in for quoted / cited refs. Returns
    one record per (card, level, PR) with the status and the raise needed.
    """
    out = []
    for d in datasets:
        for f in d["cached_cards"]:
            mentioned = {int(x) for x in re.findall(r"#(\d+)", f["card_text"])} & {
                int(n) for n in f["prs"]
            }
            for lvl in LEVELS:
                c = consts["levels"][lvl]
                dem = demand_prs(f, lvl)
                sim = simulate(f, lvl, c["count"], c["budget"])
                full = set(dem[: sim["full"]])
                cum = 0
                need_count = need_budget = 0
                cum_by = {}
                for k, n in enumerate(dem):
                    cum += f["prs"][str(n)]["block"]
                    cum_by[n] = (k + 1, cum)
                for n in sorted(mentioned):
                    if n in full:
                        status = "full"
                    elif n in dem:
                        status = "demoted by stopper"
                        need_count = max(need_count, cum_by[n][0])
                        need_budget = max(need_budget, cum_by[n][1])
                    elif any(n in i["prs"] for i in f["items"]):
                        status = "not in the level's rule (title line / roster line)"
                    else:
                        status = "not in bundle"
                    out.append(
                        dict(
                            repo=d["repo"],
                            card=f"{f['path']}:{f['start']}-{f['end']}",
                            level=lvl,
                            pr=n,
                            status=status,
                            need_count=need_count,
                            need_budget=need_budget,
                        )
                    )
    return out


def apply_quality_raises(datasets: list[dict], consts: dict) -> list[str]:
    """Raise count then budget until every cited PR a stopper demoted renders.

    Runs the free quality check, raises each level just enough (count to the
    PR's position, budget to the cumulative block size rounded up to 5,000),
    keeps values monotonic and repeats until nothing is demoted.
    """
    for lvl in LEVELS:
        c = consts["levels"][lvl]
        c["count0"], c["budget0"] = c["count"], c["budget"]
    notes: list[str] = []
    for _ in range(6):
        demoted = [
            r
            for r in quality_check(datasets, consts)
            if r["status"] == "demoted by stopper"
        ]
        if not demoted:
            break
        for r in demoted:
            c = consts["levels"][r["level"]]
            new_count = max(c["count"], r["need_count"])
            new_budget = max(c["budget"], _round_up(r["need_budget"], 5_000))
            if (new_count, new_budget) != (c["count"], c["budget"]):
                notes.append(
                    f"{r['level']}: count {c['count']}->{new_count}, budget {c['budget']:,}->{new_budget:,} "
                    f"so PR #{r['pr']} renders in full for the card {r['card']}"
                )
                c["count"], c["budget"] = new_count, new_budget
        pc = pb = 0
        for lvl in LEVELS:
            c = consts["levels"][lvl]
            c["count"], c["budget"] = max(c["count"], pc), max(c["budget"], pb)
            pc, pb = c["count"], c["budget"]
    return notes


def build_report(datasets: list[dict]) -> tuple[str, dict]:
    """Compute every item of section 13.3 and render the markdown report."""
    prices = load_prices()
    sn, ch, hk = prices["sonnet"], prices["cheapest"], prices["haiku"]
    repos = [d["repo"] for d in datasets]
    targets = _all_targets(datasets)
    consts = derive_constants(datasets)
    raise_notes = apply_quality_raises(datasets, consts)
    checks = quality_check(datasets, consts)

    # total prompt cap (3a) with both bounds applied, then monotonic (3b)
    prev = 0
    for lvl in LEVELS:
        c = consts["levels"][lvl]
        toks = [simulate(f, lvl, c["count"], c["budget"])["tokens"] for _, f in targets]
        c["max_tokens"] = max(toks)
        c["cap_tokens"] = max(prev, _round_up(max(toks), 1000))
        c["cap_chars"] = _round_up(c["cap_tokens"] * CPT, 1000)
        prev = c["cap_tokens"]

    # simulate every card once with the final constants
    sims: dict[str, dict[str, list[dict]]] = {
        r: {lv: [] for lv in LEVELS} for r in repos
    }
    for d in datasets:
        for f in d["targets"]:
            for lvl in LEVELS:
                c = consts["levels"][lvl]
                sims[d["repo"]][lvl].append(simulate(f, lvl, c["count"], c["budget"]))

    # ------- verdict
    reasons, meets = [], True
    for lvl in LEVELS:
        c = consts["levels"][lvl]
        ss = [s for r in repos for s in sims[r][lvl]]
        cut = sum(1 for s in ss if s["demoted"])
        c["final_cut"] = cut / len(ss)
        c["final_cut_n"] = cut
        if not c["budget_found"]:
            meets = False
            last = next(
                x for x in c["candidates"] if x["budget"] == BUDGET_CANDIDATES[-1]
            )
            extra = (
                f"; the smallest budget outside the rule's list that does is {c['extra_budget']:,}"
                if c["extra_budget"]
                else "; none of the extra budgets up to 80,000 does"
            )
            reasons.append(
                f"{lvl}: no candidate budget (10k-40k) cuts at most 10 % of cards "
                f"(40,000 cuts {last['cut']:.1%}){extra}"
            )
        else:
            ok_final = c["final_cut"] <= MAX_CUT_SHARE
            meets = meets and ok_final
            reasons.append(
                f"{lvl}: count ceiling {c['count']} (pooled p99 of demand), budget {c['budget']:,} chars cuts "
                f"{c['final_cut_n']}/{len(ss)} = {c['final_cut']:.1%} of cards"
                + (
                    f" (rule's own pick {c['budget0']:,}; raised by the quality check)"
                    if c["budget"] != c["budget0"]
                    else ""
                )
            )
    sep_flags = []
    for r in repos:
        for a, b in zip(LEVELS, LEVELS[1:]):
            pa = pct([s["tokens"] for s in sims[r][a]], 0.9)
            pb = pct([s["tokens"] for s in sims[r][b]], 0.9)
            if pa > (1 - SEPARATION) * pb:
                sep_flags.append(
                    f"{r}: p90 {a} {pa:,.0f} vs {b} {pb:,.0f} is {1 - pa / pb:.1%} below, needs 15 %"
                )
    unmet_quality = [x for x in checks if x["status"] == "demoted by stopper"]
    if unmet_quality:
        meets = False
        reasons.append(
            f"{len(unmet_quality)} cached-card PR mentions are still demoted by a stopper"
        )

    md = ["# B0 composition - measurement gate for plan 62 Part B", ""]
    md.append(
        f"Generated by `scripts/b0_composition.py` (read-only, no LLM calls). Repos: {', '.join(repos)}. "
        f"Tokens at {CPT} chars/token; prices from `prices.json` (as of {prices['as_of']}): Sonnet 5.5 "
        f"${sn['input']}/${sn['output']} per M, Haiku 5.5 ${hk['input']}/${hk['output']}, cheapest row of the table `{ch['model']}` ${ch['input']}/${ch['output']} per M, "
        f"{OUT_TOKENS} output tokens per card.\n"
    )

    md.append("## Proposed constants\n")
    rows = []
    ctxdef = {"low": 0, "medium": 4, "high": 6}
    for lvl in LEVELS:
        c = consts["levels"][lvl]
        rows.append(
            [
                lvl,
                c["count"],
                f"{c['budget']:,}",
                f"{c['cap_chars']:,} ({c['cap_tokens']:,} tokens)",
                ctxdef[lvl],
            ]
        )
    rows.append(["xhigh", "= high (not offered until Part D)", "= high", "= high", 8])
    md.append(
        _md_table(
            [
                "Level",
                "Count ceiling",
                "`FULL_PR_MAX_CHARS`",
                "Total prompt cap (chars)",
                "Context commits",
            ],
            rows,
        )
    )
    md.append(
        f"**Verdict - meets the decision rule: {'yes' if meets else 'no'}.** "
        + "; ".join(reasons)
        + ".\n"
    )
    if sep_flags:
        md.append(
            "Separation flags (the rule says report, R29 decides in the eval): "
            + "; ".join(sep_flags)
            + ".\n"
        )
    else:
        md.append(
            "Separation: every adjacent pair is at least 15 % apart on p90 on every repo.\n"
        )
    if raise_notes:
        md.append("Raised by the free quality check: " + "; ".join(raise_notes) + ".\n")

    # ------- Q-B3
    md.append(
        "## Open question Q-B3 - cards that lose a qualified thread to a stopper\n"
    )
    thr = sum(
        p["threads"] for d in datasets for f in d["targets"] for p in f["prs"].values()
    )
    qb = []
    for lvl in LEVELS:
        cards = sum(1 for r in repos for s in sims[r][lvl] if s["demoted"])
        total = sum(len(sims[r][lvl]) for r in repos)
        qb.append([lvl, f"{cards}/{total}"])
    if thr == 0:
        md.append(
            "**Review threads were not measurable**: the DB copies hold no review threads (Part A's columns exist after "
            "migration but are empty; filling them needs a GitHub crawl, which this gate must not run). `threads_lost` "
            "is therefore 0 by construction. The upper bound is the number of cards where a stopper demotes at least one own PR "
            "(any of those PRs may carry threads):\n"
        )
        md.append(
            _md_table(
                ["Level", "Cards with a demoted PR (upper bound of threads_lost)"], qb
            )
        )
    else:
        md.append(f"Threads present on {thr} PR blocks; see tables.\n")

    # ------- demand
    md.append("## Decision rule\n")
    md.append(
        "### Steps 1-2: demand (own PRs per card the level's rule asks for) and the count ceiling\n"
    )
    rows = []
    for lvl in LEVELS:
        c = consts["levels"][lvl]
        row = [lvl]
        for r in repos:
            row.append(_dist(c["demand_by_repo"][r]))
        row.append(_dist(c["demand"]))
        row.append(f"p95 {pct(c['demand'], 0.95):.0f}")
        row.append(c["count"])
        rows.append(row)
    md.append(
        _md_table(
            ["Level"]
            + [f"{r} p50/p90/p99/max" for r in repos]
            + [
                "pooled p50/p90/p99/max",
                "pooled",
                "Count ceiling (pooled p99, monotonic)",
            ],
            rows,
        )
    )

    md.append(
        "### Step 3: character budget candidates (count ceiling applied, stop-at-first)\n"
    )
    for lvl in LEVELS:
        c = consts["levels"][lvl]
        md.append(f"**{lvl}** (count ceiling {c['count']} before quality raises)\n")
        md.append(
            _md_table(
                ["Budget (chars)", "Cards cut", "Share", "p90 tokens", "p99 tokens"],
                [
                    [
                        f"{x['budget']:,}",
                        x["cut_n"],
                        f"{x['cut']:.1%}",
                        f"{x['p90']:,.0f}",
                        f"{x['p99']:,.0f}",
                    ]
                    for x in c["candidates"]
                ],
            )
        )
    md.append(
        "Candidates are evaluated with the pre-quality-check count ceiling; the table above shows the final constants.\n"
    )

    md.append("### Steps 3a-3b: total prompt cap and monotonic values\n")
    rows = []
    for lvl in LEVELS:
        c = consts["levels"][lvl]
        rows.append(
            [
                lvl,
                f"{c['max_tokens']:,.0f}",
                f"{c['cap_tokens']:,}",
                f"{c['cap_chars']:,}",
            ]
        )
    md.append(
        _md_table(
            [
                "Level",
                "Sample max tokens (both bounds applied)",
                "Cap tokens (rounded up, monotonic)",
                "Cap chars",
            ],
            rows,
        )
    )
    md.append(
        "With about 100 targets per repo the sample maximum is the p99 neighbourhood, so the total cap binds on 0 sampled cards by construction; it guards what no rule anticipates.\n"
    )

    # ------- stoppers touched + separation + tokens/prices
    md.append("### Steps 4-5: per repo tokens, price, stoppers touched, separation\n")
    rows = []
    for r in repos:
        for lvl in LEVELS:
            ss = sims[r][lvl]
            tk = [s["tokens"] for s in ss]
            n = len(ss)
            rows.append(
                [
                    r,
                    lvl,
                    f"{pct(tk, 0.5):,.0f} / {pct(tk, 0.9):,.0f} / {pct(tk, 0.99):,.0f}",
                    f"${card_price(pct(tk, 0.5), sn):.3f} / ${card_price(pct(tk, 0.9), sn):.3f} / ${card_price(pct(tk, 0.99), sn):.3f}",
                    f"${card_price(pct(tk, 0.5), hk):.4f} / ${card_price(pct(tk, 0.9), hk):.4f} / ${card_price(pct(tk, 0.99), hk):.4f}",
                    f"${card_price(pct(tk, 0.5), ch):.4f} / ${card_price(pct(tk, 0.9), ch):.4f} / ${card_price(pct(tk, 0.99), ch):.4f}",
                    f"{statistics.mean(tk):,.0f}",
                    f"{sum(1 for s in ss if s['stop'] == 'count') / n:.1%}",
                    f"{sum(1 for s in ss if s['stop'] == 'budget') / n:.1%}",
                    f"{sum(1 for s in ss if s['chars'] > consts['levels'][lvl]['cap_chars']) / n:.1%}",
                ]
            )
    md.append(
        _md_table(
            [
                "Repo",
                "Level",
                "Tokens p50/p90/p99",
                "Sonnet 5.5 $ p50/p90/p99",
                "Haiku 5.5 $ p50/p90/p99",
                f"{ch['model']} $ p50/p90/p99",
                "Mean tokens",
                "Count ceiling binds",
                "Budget binds",
                "Total cap binds",
            ],
            rows,
        )
    )
    rows = []
    for r in repos:
        for a, b in zip(LEVELS, LEVELS[1:]):
            pa = pct([s["tokens"] for s in sims[r][a]], 0.9)
            pb = pct([s["tokens"] for s in sims[r][b]], 0.9)
            rows.append(
                [
                    r,
                    f"{a} vs {b}",
                    f"{pa:,.0f}",
                    f"{pb:,.0f}",
                    f"{1 - pa / pb:.1%}",
                    "ok" if pa <= (1 - SEPARATION) * pb else "FLAG near-identical",
                ]
            )
    md.append(
        "**Separation check (p90 input tokens, each level at least 15 % below the next)**\n"
    )
    md.append(
        _md_table(["Repo", "Pair", "p90 lower", "p90 upper", "Gap", "Result"], rows)
    )

    # ------- composition per level
    md.append("## Report items (section 13.3)\n")
    md.append(
        "### Tokens per block kind (p50 / p90, per repo and level; threads = Part A, 0 where not measurable)\n"
    )
    kinds = [
        "commit_text",
        "squash_body_placeholder",
        "pr_body",
        "roster",
        "discussion",
        "issues",
        "descriptions",
        "symbol_context",
        "context",
        "pr_title_line",
        "roster_only_line",
        "pr_other",
    ]
    for r in repos:
        rows = []
        for k in kinds:
            row = [k]
            for lvl in LEVELS:
                xs = [s["comp"].get(k, 0) / CPT for s in sims[r][lvl]]
                row.append(f"{pct(xs, 0.5):,.0f} / {pct(xs, 0.9):,.0f}")
            rows.append(row)
        rows.append(["part_a_threads", *["0 / 0 (not measurable)"] * 3])
        md.append(f"**{r}**\n")
        md.append(_md_table(["Block"] + list(LEVELS), rows))

    md.append("### Line-level counts, own PRs, PR blocks, compact context, seconds\n")
    rows = []
    for d in datasets:
        fs = d["targets"]
        by_src = Counter(i["src"] for f in fs for i in f["items"])
        line_n = [f["line_n"] for f in fs]
        raw_n = [f["line_total"] for f in fs]
        own_blame = [len(demand_prs(f, "low")) for f in fs]
        own_all = [len(demand_prs(f, "medium")) for f in fs]
        ctx_all = [c / CPT for s in sims[d["repo"]]["high"] for c in s["ctx_chars"]]
        pb = [
            s["pr_blocks"] / s["pr_unique"]
            for s in sims[d["repo"]]["medium"]
            if s["pr_unique"]
        ]
        tpr = [f["today_items"] / CPT + (SYS_CHARS + f["symctx"]) / CPT for f in fs]
        rows.append(
            [
                d["repo"],
                len(fs),
                f"{sum(1 for f in fs if f.get('important'))}",
                ", ".join(f"{k} {v}" for k, v in sorted(by_src.items())),
                f"{pct(line_n, 0.5):.0f} / {pct(line_n, 0.9):.0f} / {max(line_n)} (uncapped max {max(raw_n)})",
                _dist(own_blame),
                _dist(own_all),
                f"{statistics.mean(pb):.2f}" if pb else "-",
                f"{pct(ctx_all, 0.5):.0f} / {pct(ctx_all, 0.9):.0f}"
                if ctx_all
                else "-",
                f"{statistics.mean(f['secs'] for f in fs):.2f}",
                f"{pct(tpr, 0.5):,.0f} / {pct(tpr, 0.9):,.0f}",
            ]
        )
    md.append(
        _md_table(
            [
                "Repo",
                "Targets",
                "Important symbols (>=5 callers)",
                "Items by source (all targets)",
                "Line-level units p50/p90/max",
                "Own PRs of blame units p50/p90/p99/max",
                "Own PRs of all units p50/p90/p99/max",
                "PR blocks per unique PR (today's rendering, medium set)",
                "Tokens per compact context commit p50/p90",
                "Seconds per target (collection)",
                "Today's prompt tokens p50/p90",
            ],
            rows,
        )
    )

    # strata
    rows = []
    for d in datasets:
        c = Counter(f["stratum"] for f in d["targets"])
        rows.append(
            [
                d["repo"],
                c["<5"],
                c["5-19"],
                c[">=20"],
                "yes" if d["has_codegraph"] else "no (regex Rust `fn` targets)",
            ]
        )
    md.append("**Target strata**\n")
    md.append(
        _md_table(["Repo", "<5 commits", "5-19", ">=20", "CodeGraph DB used"], rows)
    )

    # ablations
    md.append("### Per-level tokens with and without each Part B change (mean / p90)\n")
    toggles = [
        ("all Part B rules", {}),
        ("without stoppers", {"stoppers": False}),
        ("without PR-once", {"pr_once": False}),
        ("without merge/squash body drop", {"drop_body": False}),
        ("without own-PR rule (all linked PRs full)", {"own_rule": False}),
        ("without context ring", {"ring1": False}),
    ]
    for r in repos:
        rows = []
        fs = [d for d in datasets if d["repo"] == r][0]["targets"]
        today = [(f["today_items"] + SYS_CHARS + f["symctx"]) / CPT for f in fs]
        rows.append(
            [
                "today (limit 20, no Part B)",
                *[f"{statistics.mean(today):,.0f} / {pct(today, 0.9):,.0f}"] * 3,
            ]
        )
        for name, kw in toggles:
            row = [name]
            for lvl in LEVELS:
                c = consts["levels"][lvl]
                tk = [
                    simulate(f, lvl, c["count"], c["budget"], **kw)["tokens"]
                    for f in fs
                ]
                row.append(f"{statistics.mean(tk):,.0f} / {pct(tk, 0.9):,.0f}")
            rows.append(row)
        md.append(f"**{r}**\n")
        md.append(_md_table(["Variant"] + list(LEVELS), rows))

    # cold cost
    md.append(
        "### Cold description cost per level (cards with undescribed commits; Sonnet 5.5 rates; a floor where git numstat is unavailable)\n"
    )
    rows = []
    for r in repos:
        for lvl in LEVELS:
            ss = sims[r][lvl]
            cost = [
                (s["cold_in"] * sn["input"] + s["cold_out"] * sn["output"]) / 1e6
                for s in ss
            ]
            cost_c = [
                (s["cold_in"] * ch["input"] + s["cold_out"] * ch["output"]) / 1e6
                for s in ss
            ]
            cold_share = sum(1 for s in ss if s["undescribed"]) / len(ss)
            rows.append(
                [
                    r,
                    lvl,
                    f"{statistics.mean(s['undescribed'] for s in ss):.1f}",
                    f"${statistics.mean(cost):.3f} / ${pct(cost, 0.9):.3f}",
                    f"${statistics.mean(cost_c):.4f}",
                    f"{cold_share:.0%}",
                ]
            )
    md.append(
        _md_table(
            [
                "Repo",
                "Level",
                "Undescribed commits per card (mean)",
                "Cold describe $ mean / p90 (Sonnet 5.5)",
                f"mean ({ch['model']})",
                "Cards that are cold = `partial`, regenerate once (second card call)",
            ],
            rows,
        )
    )

    # cache replay
    md.append(
        "### Cache invalidation, last 90 days replayed over the sampled targets\n"
    )
    md.append(
        "Per (target, commit touching the file in the 90 days before HEAD). **Today** a commit is a miss when the 20-commit bundle changes (any file commit while the target has fewer than 20 line-level commits) or the range shifts; **Ring 0 only** a commit is a miss when it touches the target's lines (its SHA is among the line-level SHAs at HEAD; a lower bound, a commit whose lines were rewritten again inside the window counts as elsewhere). A line shift is a hunk above the target that changes the line count (HEAD positions, approximate).\n"
    )
    rows = []
    for d in datasets:
        pairs = today = ring0 = shifts = tgt_today = tgt_ring0 = 0
        three = 0
        for f in d["targets"]:
            rp = f.get("replay")
            if not rp or not rp["commits"]:
                continue
            n = rp["commits"]
            pairs += n
            fill = f["line_n"] < COLLECT_LIMIT
            t_miss = rp["touch"] + rp["shift"] + (rp["elsewhere"] if fill else 0)
            today += t_miss
            ring0 += rp["touch"]
            shifts += rp["shift"]
            tgt_today += t_miss > 0
            tgt_ring0 += rp["touch"] > 0
            three += (rp["shift"] + rp["elsewhere"]) >= 3
        nt = len(d["targets"])
        rows.append(
            [
                d["repo"],
                pairs,
                f"{today / pairs:.1%}" if pairs else "-",
                f"{ring0 / pairs:.1%}" if pairs else "-",
                f"{shifts / today:.1%}" if today else "-",
                f"{tgt_today}/{nt}",
                f"{tgt_ring0}/{nt}",
                f"{three}/{nt}",
            ]
        )
    md.append(
        _md_table(
            [
                "Repo",
                "(target, commit) pairs",
                "Misses today",
                "Misses Ring 0 only",
                "Line shifts as share of today's misses",
                "Targets missed >=1x today",
                "Targets missed >=1x Ring 0",
                "Targets with >=3 other file commits (R39 context rebuild)",
            ],
            rows,
        )
    )

    # own-PR coverage
    md.append("### Own-PR coverage on blame units (section 13.3 last bullet)\n")
    rows = []
    for d in datasets:
        units = noown = ro_nonrel = ro_any = 0
        for f in d["targets"]:
            prs = f["prs"]
            for u in _units(f, False):
                if not u["blame"]:
                    continue
                units += 1
                if _own(u["head"], prs):
                    continue
                noown += 1
                linked = u["head"]["prs"]
                ro_any += bool(linked)
                ro_nonrel += any(not prs[str(n)]["release"] for n in linked)
        rows.append(
            [
                d["repo"],
                "merge-based" if d["merge_based"] else "squash/other",
                units,
                noown,
                f"{noown / units:.1%}" if units else "-",
                ro_any,
                ro_nonrel,
                f"{ro_nonrel / units:.1%}" if units else "-",
            ]
        )
    md.append(
        _md_table(
            [
                "Repo",
                "History",
                "Blame units",
                "With no own PR",
                "Share",
                "...with any roster PR",
                "...with a non-release roster PR",
                "Share of blame units a 'earliest-merged roster PR' fallback would add",
            ],
            rows,
        )
    )
    md.append(
        "Release / sync PRs are title matches of `release`, `sync`, `back-merge`, `promote`, `vX.Y`, `chore(release)`, `bump ... version`. Material = more than about 5 % of blame units.\n"
    )

    # context commits cost
    md.append("### Context commits cost\n")
    rows = []
    for r in repos:
        for lvl in ("medium", "high"):
            ss = sims[r][lvl]
            cc = [s["comp"].get("context", 0) / CPT for s in ss]
            allc = [x / CPT for s in ss for x in s["ctx_chars"]]
            rows.append(
                [
                    r,
                    lvl,
                    f"{pct(cc, 0.5):,.0f} / {pct(cc, 0.9):,.0f} / {max(cc):,.0f}",
                    f"{pct(allc, 0.5):,.0f} / {pct(allc, 0.9):,.0f}" if allc else "-",
                    f"${card_price(statistics.mean(cc), sn) - card_price(0, sn):.4f}",
                ]
            )
    md.append(
        _md_table(
            [
                "Repo",
                "Level",
                "Context block tokens p50/p90/max",
                "Per commit p50/p90",
                "Mean Sonnet 5.5 input cost of the ring",
            ],
            rows,
        )
    )
    allctx = [s["comp"].get("context", 0) for r in repos for s in sims[r]["high"]]
    md.append(
        f"Proposed `[rationale].context_max_chars` default: {_round_up(pct(allctx, 0.9), 500):,} (pooled p90 of the six-commit ring in characters, rounded up); defaults 0 / 4 / 6 / 8 stay until the Part C eval.\n"
    )

    # quality check
    md.append("### Free quality check (cached cards)\n")
    cards = [(d["repo"], f) for d in datasets for f in d["cached_cards"]]
    md.append(
        f"{len(cards)} cached cards exist in the DB copies ({', '.join(f'{r}: {sum(1 for rr, _ in cards if rr == r)}' for r in repos)}). They store items as plain strings without refs, so `quoted`/`cited` refs are not available; "
        "`#n` mentions in the card text that match a PR of the rebuilt bundle stand in.\n"
    )
    if checks:
        md.append(
            _md_table(
                ["Repo", "Card", "Level", "PR", "Status under the proposed constants"],
                [
                    [c["repo"], c["card"], c["level"], f"#{c['pr']}", c["status"]]
                    for c in checks
                ],
            )
        )
    else:
        md.append("No card mentions a PR that is in its rebuilt bundle.\n")
    cc = []
    for r, f in cards:
        cc.append(
            [
                r,
                f"{f['path']}:{f['start']}-{f['end']}",
                f"{f['card_tokens']:,}",
                f"{simulate(f, 'medium', **{'count_cap': consts['levels']['medium']['count'], 'budget': consts['levels']['medium']['budget']})['tokens']:,.0f}",
                f["card_model"],
            ]
        )
    md.append(
        "Recorded input tokens of those cards vs the same target at `medium` under the proposed rules (the card was built with today's rendering at its own date, so only the order of magnitude is comparable):\n"
    )
    md.append(
        _md_table(
            [
                "Repo",
                "Card",
                "Recorded input tokens",
                "medium (this model)",
                "Card model",
            ],
            cc,
        )
    )

    md.append("## Method and limits\n")
    md.append(
        "- Targets: ~100 per repo, quotas 15 / 35 / 50 over file-history strata (<5, 5-19, >=20 commits), "
        "plus 10 symbols with >= 5 distinct `calls` callers where a CodeGraph copy exists; seed 62. "
        "Biome had no CodeGraph copy (its 491 MB index could not be copied; the disk was full), so its targets are "
        "regex-found Rust `fn` ranges in `crates/*/src` (no symbol context, no important-symbol sample).\n"
        "- All databases are copies; the whygraph and axios copies and the biome copy were migrated to schema head "
        "(the axios copy was hand-patched by an earlier review and re-stamped at `5e8a2c9d1f47` first).\n"
        "- Collection uses `collect_evidence(limit=40)` then applies section 13.4's origin guarantee at 20 "
        "(Part B1 is not implemented yet); the repo working tree is read-only (`git blame`).\n"
        "- Description chars for commits without a stored description use `portal/estimate.estimate_tokens` output "
        "x 3.5 x 0.6 (stored descriptions measured 0.6x the estimate on 283 axios commits); commits with no "
        "`commit_file_change` rows are sized from `git diff --numstat`. The whygraph (gate) copy stores no descriptions at "
        "all, so every whygraph card there is cold; the biome copy stores 3.\n"
        "- Full-PR block = `_format_pr` (body, roster, discussion), issues of full PRs rendered once; Part A thread "
        "text is 0 (not measurable). Stopper 1 stops at the FIRST PR that does not fit (count or characters); the "
        "seed `sim_adapt2.py` skipped and continued.\n"
        "- The whygraph-gate repo's whole history (299 commits) falls inside the 90-day replay window, so its Ring 0 "
        "miss rate is high by construction.\n"
        "- Cards at the boundary: a level whose final cut share is within one point of 10 % is a knife edge; "
        "changing the sample by a card or two flips the verdict.\n"
    )
    return "\n".join(md), consts


def cmd_report(args: argparse.Namespace) -> None:
    """Replay the rules over stored facts and write the markdown report."""
    datasets = [json.loads(Path(p).read_text()) for p in args.facts]
    text, consts = build_report(datasets)
    Path(args.out).write_text(text)
    if args.constants:
        Path(args.constants).write_text(
            json.dumps(
                {
                    k: {
                        x: y
                        for x, y in v.items()
                        if x not in ("demand", "demand_by_repo", "candidates")
                    }
                    for k, v in consts["levels"].items()
                },
                indent=1,
            )
        )
    print("wrote", args.out)


def cmd_replay(args: argparse.Namespace) -> None:
    """Add the 90-day cache replay to stored facts (needs only the repo's git)."""
    data = json.loads(Path(args.facts).read_text())
    root = data["root"]
    head = _git(root, "log", "-1", "--format=%cI").strip()
    for f in data["targets"]:
        shas = {i["sha"] for i in f["items"] if i["src"] in LINE_SOURCES}
        f["replay"] = _replay_window(root, f, shas, head)
    Path(args.facts).write_text(json.dumps(data))
    print("replayed", len(data["targets"]), "targets in", args.facts)


def main() -> None:
    """Parse the sub-command and run it."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate")
    m.add_argument("--wdb", required=True)
    m.set_defaults(fn=cmd_migrate)
    c = sub.add_parser("collect")
    c.add_argument("--repo", required=True)
    c.add_argument("--root", required=True)
    c.add_argument("--wdb", required=True)
    c.add_argument("--cdb")
    c.add_argument("--n", type=int, default=100)
    c.add_argument("--merge-based", action="store_true")
    c.add_argument("--out", required=True)
    c.set_defaults(fn=cmd_collect)
    rp = sub.add_parser("replay")
    rp.add_argument("--facts", required=True)
    rp.set_defaults(fn=cmd_replay)
    r = sub.add_parser("report")
    r.add_argument("--facts", nargs="+", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--constants")
    r.set_defaults(fn=cmd_report)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
