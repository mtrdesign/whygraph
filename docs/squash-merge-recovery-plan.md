# Squash-Merge PR Commit Recovery — Implementation Plan

> **Status: Reviewed — scope locked (all §0 decisions folded in) + consistency pass applied. Awaiting final approval before any code.**
> Nothing in this document is implemented yet. Each phase is reviewed and gated separately;
> no code is written until this plan is approved.
> **Consistency pass (2026-06-11):** corrected config placement — the three Stage-0 rendering caps
> live in `RationaleConfig` (`[rationale]`), not `AnalyzeConfig`, because `RationaleGenerator` is
> built from `RationaleConfig` (`config.py:256`); only the enrichment gate `pr_origin_min_commits`
> sits in `AnalyzeConfig`. Fixed the §4.8 per-line trigger (it must fire for *every* enriched squash,
> not only file-bulk ones). Added skeleton snippets for the two NEW modules (§4.5, §4.8).
> **Goal:** when a feature PR is squash-merged into the default branch, restore WhyGraph's
> analyzing power over the *original* feature-branch commits — surface them (and the PR
> discussion) as evidence, and attribute each line of the squash commit back to the specific
> original commit that introduced it.
> **Date:** 2026-06-11

---

## 0. Resolved scope decisions (from the requester)

| Topic | Decision |
|---|---|
| Scope | **Stage 0 + Stage 1.** Stage 2 (graph-aware fallback splitter for local-only / GC'd history) is **out of scope** for this plan. |
| Attribution | **Per-line mapping required** — map each line of the squash `merge_commit_sha` back to the original PR commit that introduced it, not just PR-level evidence. |
| Provider | **GitHub only** for Stage 1 (the only provider WhyGraph integrates today — `services/github/`). Azure/GitLab `refs/pull` equivalents are a later provider-abstraction concern. |
| Link storage | **No PR↔commit table and no `pr_id` column.** Reuse the existing `commit_titles`/`_linked_prs` linkage (many-to-many safe); only the `commit.on_default_branch` flag is new (§4.3). |
| `commit_titles` retention | **Keep for now**, remove in a later separate migration once `_linked_prs` and Stage 0 are repointed. |
| Stage-0 caps | **Make them `whygraph.toml`-configurable** under **`[rationale]`** (consumed by the rationale generator) and document them in the example config so developers can tune roster/discussion size (§4.2, §4.11). |
| Enrichment cadence | **Remote `scan` phase only** (forced by the offline-MCP / `--no-remote` invariants); diffs/descriptions stay lazy (§3.1, §4.7). |
| Enrichment gate | **Balanced:** squash-detected **AND** (`files_changed > large_commit_file_count` **OR** `len(commit_titles) >= pr_origin_min_commits`, default 5, configurable) (§3.3, §4.11). |
| Fetch strategy | **Targeted batched** — one `git fetch` carrying only the gated candidates' refspecs; local ref count == #candidates, not #all-PRs (§3.1). |
| Enrichment default | **On by default**; `--no-pr-origins` disables, always skipped under `--no-remote` (§4.6). |

---

## 1. Goals & Non-Goals

### Goals
- **Stage 0** — stop discarding data WhyGraph *already stores*: inline a squash PR's
  `commit_titles` and `comments` into the evidence bundle and the rationale prompt.
- **Stage 1** — fetch each squash-merged PR's **original feature-branch commits** (full message at
  scan, diff lazily), persist them as `Commit` rows linked to their PR via the existing
  `commit_titles` (no new relation), and make them first-class evidence.
- **Per-line attribution** — when a blame hit lands on a squash commit, re-attribute the queried
  line range to the original PR commits that authored those lines.

### Non-Goals
- **Stage 2 / LLM or graph-based diff splitting** — *out of scope.* When no provider data exists
  (local-only history, GC'd branch, non-PR squash), the commit keeps today's bulk-stub behaviour.
  Reconstructing boundaries from the diff is explicitly deferred.
- **Non-GitHub providers** — Azure DevOps / GitLab have no `refs/pull/*/head`; their enrichment is
  deferred. Stage 0 is provider-agnostic and benefits them for free.
- **Re-describing the squash commit whole-diff** — the bulk-stub path (`scan/analyze_crawler.py`,
  `analyze/backfill.py`) is **unchanged on purpose**; we add a parallel origin-commit signal, we do
  not resurrect the expensive whole-squash-diff LLM call.
- **Changing the first-parent main-branch walk** — `scan/git_crawler.py`'s notion of "a commit on
  the default branch" is untouched; origin commits are tagged so they never leak into the
  main-walk-only queries (area-history, refactor-walk).

---

## 2. How the system works today (reference map)

| Concern | Location | Current behaviour |
|---|---|---|
| PR ingestion | `services/github/pull_requests.py:43-56` | GraphQL already pulls `commits(first: 250)` (oid, headline, author) **and** `comments(first: 100)`. |
| PR value object | `services/github/pull_request.py:97-115` | `commits: tuple[CommitSummary,...]`, `comments: tuple[Comment,...]` already parsed. |
| PR persistence | `scan/github_crawler.py:92-129` | `commit_titles` ← JSON of `{oid, headline, author_*}`; `comments` ← JSON of `{author, body, created_at}`. **Both stored, neither surfaced.** |
| PR row | `db/models/pull_request.py:33,41-46` | has `merge_commit_sha` (indexed), `head_sha`, `commit_titles`, `comments`. **No merge-method field.** |
| Squash→PR link | `mcp/evidence.py:81-101` (`_linked_prs`) | matches `merge_commit_sha == sha` OR `head_sha == sha` OR oid in `commit_titles`. A squash commit **is** the `merge_commit_sha`, so the link already works. |
| Evidence serialization | `mcp/evidence.py:465-476` (`_pr_dict`) | emits only number/title/body/state/merged_at/author/html_url/labels — **drops `commit_titles` and `comments`.** |
| Rationale prompt | `analyze/rationale_generator.py:93-102` (`_format_pr`) | renders only PR title + body — **drops commits and comments.** |
| Evidence join | `mcp/evidence.py:200-217` | per blame hunk: `session.get(Commit, hunk.sha)`; drops the hunk if no `Commit` row exists. |
| Blame at a rev | `services/git/repository.py:190-258` + `commands.py:125-209` | `blame(path, a, b, rev=<sha>)` already supported; **predecessor-blame uses `rev=parent_sha`** (`evidence.py:299-324`). `-w -M -C` always on. |
| Source labels/priority | `evidence.py:39-44`, `rationale_generator.py:119-124`, `rationale.py:37-62` | `blame > blame-walked > predecessor-blame > area`; each label has a human string. |
| Commit row | `db/models/commit.py:11-32` | PK `sha`; has `parent_shas`, `files_changed`, `refactor_score`, `llm_description`. **No origin discriminator.** |
| PR→commit linkage | `mcp/evidence.py:69-101` (`_commit_titles_contain_oid`, `_linked_prs`) | already resolves a commit to its PR(s) by oid-in-`commit_titles`; returns *all* matching PRs (many-to-many safe). Reused instead of a new link table (see §4.3). |
| Lazy diff/description | `analyze/backfill.py` (`backfill_file_description`, `backfill_all`); `evidence.py:362-449` | per-file diff via `Repository.diff(commit, pathspec=...)`, cached on the `commit_file_change` row. The reuse target for origin-commit diffs. |
| Model registration | `db/models/__init__.py:31-47` | a new model must be imported here for Alembic autogenerate to see it. |

**Key reuse insight (de-risks per-line attribution):** git already does hunk-matching. A squash
merge applies the PR's `base..head` diff as one commit, so for a changed file the squash tree
equals `head_sha`'s tree. Blaming the *same* `path:line_start-line_end` at **`rev=head_sha`** maps
each line to the original PR commit — exactly the mechanism predecessor-blame already uses with
`rev=parent_sha`. No bespoke hunk re-diffing is required; we add a branch, not an algorithm.

---

## 3. Issues found / foreseen problems

### 3.1 Original PR commits are not in the local object store (Us / enabling)
After a normal clone, `refs/pull/*/head` are **not** fetched, so the squashed feature-branch
commits aren't local — `git blame rev=head_sha` and `Repository.diff` would fail on unknown SHAs.
- **Mitigation:** during the `scan` GitHub phase (already remote), one **targeted batched** fetch
  brings only the gated candidates (§3.3): a single `git fetch origin` call carrying one
  `refs/pull/<N>/head:refs/whygraph/pull/<N>` refspec per candidate PR. One round-trip, and the local
  ref count equals #candidates — **not** the wildcard `refs/pull/*` (which would litter `.git` with a
  ref per PR and slow every git op on large repos). Storing under our own `refs/whygraph/pull/*`
  namespace pins the objects against local GC. All later blame/diff is then **offline** — preserving
  the MCP-server and `--no-remote` git-hook invariants (no network at query time).
- **Required from provider:** GitHub retains `refs/pull/N/head` indefinitely (survives branch
  deletion). True for GitHub; **not** for Azure/GitLab → why they're deferred.

### 3.2 Squash tree may not exactly equal `head_sha` (Mixed / Low)
"Rebase and merge", or a squash re-applied onto an advanced base, can shift surrounding context.
Within a changed file the *line content* still matches (same diff), so `git blame`'s own
move/whitespace-tolerant matching (`-w -M -C`) absorbs the drift.
- **Mitigation:** per-line re-blame is **best-effort**, mirroring predecessor-blame (`evidence.py:314-323`
  swallows `GitError` per event). On mismatch we keep PR-level evidence (Stage 1) — never worse than today.

### 3.3 DB volume if every PR's commits are stored (Us / Medium)
5,000 PRs × ~30 commits = ~150k extra `commit` rows if we enriched every merged PR.
- **Mitigation — balanced gate (resolved Q2).** Enrich a PR only when it is squash-detected (§3.5)
  **and** either of the two distinct loss signals fires:
  - **file-bulk** — `merge_commit_sha`'s `files_changed > analyze.large_commit_file_count` (a "huge
    commit" — the squash that lost the most *description* fidelity, the stub case); **or**
  - **commit-rich** — `len(commit_titles) >= analyze.pr_origin_min_commits` (default 5) — a squash
    that collapsed many commits' worth of *narrative*, even if few files changed.
  File-count and commit-count are **distinct signals**: the first is about description cost, the
  second about how much per-commit rationale was destroyed. The `OR` catches both; the threshold knob
  (§4.11) caps volume. Small single-/few-commit PRs that merged as their own commits already work via
  the existing `commit_titles` link and are skipped.

### 3.4 Origin commits must not pollute main-walk queries (Us / High)
`commit` today means "first-parent walk of the default branch". area-history (`path_history.py`) and
refactor-walk (`_boring_shas_in`, `evidence.py:286-296`) assume that.
- **Mitigation:** add an `on_default_branch` discriminator to `commit` (default `1`); origin commits
  insert as `0`. Gate area-history and refactor-walk on `on_default_branch == 1`. The per-line
  blame-at-`head_sha` path resolves origin commits via `session.get(Commit, sha)` regardless.

### 3.5 No stored merge method (Them→Us / Low)
GraphQL gives `mergeCommit{oid}` but WhyGraph stores no squash/rebase/merge flag.
- **Mitigation:** we don't need the method. **Squash detection** = a merged PR whose `commit_titles`
  oids are **absent from the `commit` table** (i.e. the originals are not on the main walk). That PR
  then passes the §3.3 balanced gate (file-bulk OR commit-rich) to be enriched. Self-correcting: if
  the originals *are* on main (a plain merge / rebase), the normal path already wins and we skip.

---

## 4. Detailed changes (file by file)

### Phase 0 (Stage 0) — surface already-stored data

#### 4.1 `mcp/evidence.py` — `_pr_dict` (line 465)
Add the two dropped fields (decode with the existing `_json_list` helper, line 58):
```python
def _pr_dict(pr: PullRequest) -> dict:
    return {
        # ...existing keys unchanged...
        "labels": _json_list(pr.labels),
        "commit_titles": _json_list(pr.commit_titles),  # NEW
        "comments": _json_list(pr.comments),             # NEW
    }
```
No new query — both columns are already on the loaded `PullRequest` row (and already detached via
`session.expunge_all()` at `evidence.py:217`). **Intentionally uncapped:** `_pr_dict` feeds the raw
`whygraph_evidence_for` JSON, whose consumer is an agent that can handle the full list — the
context-budget caps (§4.2) apply only to the LLM *rationale prompt*, not to this tool output.

#### 4.2 `analyze/rationale_generator.py` — `_format_pr` (line 93)
After the body block, append a compact commit roster and the discussion so the LLM sees the
narrative that the squash destroyed. Reuse `_indent_block` (line 69) and the JSON-decode idiom from
`_labels_suffix` (line ~57) — add a small local `_json_list` (mirror `evidence.py:58`). The three
caps are passed in (see threading note below), not read globally:
```python
def _format_pr(pr: PullRequest, caps: RationaleConfig) -> list[str]:   # or a small caps tuple
    # ...existing title/body lines unchanged...
    titles = _json_list(pr.commit_titles)[: caps.pr_roster_max_commits]
    if titles:
        lines.append("    Squashed commits:")
        for c in titles:
            lines.append(f"      - {c.get('headline','')}  ({(c.get('oid') or '')[:9]})")
    comments = _json_list(pr.comments)[: caps.pr_discussion_max_comments]
    if comments:
        lines.append("    Discussion:")
        for cm in comments:
            who = cm.get("author") or "unknown"
            body = (cm.get("body") or "").strip()[: caps.pr_comment_max_chars]
            lines.append(_indent_block(f"[{who}] {body}", "      "))
    return lines
```
**Execution gotcha:** `from_config` today *discards* the `RationaleConfig` after pulling
`provider`/`model`/`timeout_sec` (`rationale_generator.py:393`). To make caps reach `_format_pr`,
retain the three cap ints (or the whole `RationaleConfig`) on the generator in `__init__`, then pass
them into `_format_evidence(evidence, caps)` at the call site (line 436).
**Config source & threading (corrected in the consistency pass).** The caps come from
**`RationaleConfig`** (the `[rationale]` table — §4.11), **not** `AnalyzeConfig`: the generator is
built via `RationaleGenerator.from_config(get_config().rationale)` (`rationale_generator.py:360-393`),
so `AnalyzeConfig` never reaches it. `_format_pr`/`_format_evidence` are **module-level** functions
called from `generate()` (`bundle = _format_evidence(evidence)`, line 436). Thread the caps by:
storing them on the generator in `from_config`/`__init__`, then passing a small `caps` value into
`_format_evidence(evidence, caps)` → `_format_pr(pr, caps)`. Keeping them as a parameter (not a
global `get_config()` reach-in) keeps the formatters pure and unit-testable. **Bounding rationale
(resolved Q4):** the rationale bundle is already unbounded (the documented "evidence-bundle builder"
TODO in this module), so this stays a prompt-size guard, not a feature — but developers can tune it.

### Phase 1 (Stage 1) — provider enrichment + persistence

#### 4.3 No PR↔commit link table — reuse the existing `commit_titles` linkage
We do **not** add a join table. `_linked_prs()` (`mcp/evidence.py:81-101`) already resolves a
commit to its PR(s) by matching the oid inside the PR's `commit_titles` JSON
(`_commit_titles_contain_oid`, line 69). Recovered origin commits *come from* `commit_titles`, so
once inserted as `Commit` rows the existing query finds their PR(s) for free — and because it
returns **all** matching PRs, the many-to-many edge (a commit shared across stacked / backport PRs)
is handled without a column or a join table. A single `pr_id` column on `commit` was rejected: it
cannot represent that many-to-many and would silently drop links. Commit ordering is taken from
`committed_at` (evidence is already sorted that way, `evidence.py:231`), so no `position` field is
needed either. This is the "don't add the abstraction until a second concrete case forces it" rule
(CLAUDE.md) applied: nothing in scope queries "all original commits of PR #N" relationally.

#### 4.4 `db/models/commit.py` — origin discriminator (line 32)
```python
    on_default_branch: int = Field(
        default=1, sa_column_kwargs={"server_default": text("1")}
    )  # 0 = recovered PR-origin commit, not on the first-parent main walk
```
`int` 0/1 to keep SQLite INTEGER affinity (same rationale as `PullRequest.draft`,
`pull_request.py:20-22`).

#### 4.5 NEW: `scan/pr_origin_enricher.py`
A scan sub-phase (driven from `scan/crawler.py`, after `github_crawler` so PR rows exist).
**Candidate selection (§3.3, §3.5):** a merged PR is a candidate when its `commit_titles` oids are
absent from `commit` (squash) **and** (`merge_commit_sha`'s `files_changed > large_commit_file_count`
**or** `len(commit_titles) >= pr_origin_min_commits`). Then:
1. Ensure objects are local: **one targeted batched** `git fetch origin <refspec…>` carrying a
   `refs/pull/<N>/head:refs/whygraph/pull/<N>` refspec for **each candidate PR only** (not the
   `refs/pull/*` wildcard) — one round-trip, candidate-many local refs.
2. For each oid in `commit_titles`: insert a `Commit` row (`on_default_branch=0`) — parse via the
   existing `git log -1 <oid>` path (reuse `GitLogShortstatCmd` shape / `Commit.from_git_log`,
   `commands.py:250-279`) so full message body + stats come from git, not just the headline.
3. Leave `llm_description` NULL — diffs/descriptions are lazy (4.7).
No link row is written — the PR↔commit association is already carried by `commit_titles` and
resolved at query time by `_linked_prs` (4.3). Idempotent: skip oids already present in `commit`
(mirror `github_crawler.py:67-75`).

Skeleton (mirrors `GitHubCrawler.work` session/idempotency shape, `github_crawler.py:59-89`):
```python
def enrich(repo: Repository, *, min_commits: int, large_commit_file_count: int) -> None:
    with get_session() as session:
        existing: set[str] = set(session.exec(select(Commit.sha)).all())
        candidates = []
        for pr in session.exec(select(PullRequest).where(col(PullRequest.merged_at).is_not(None))):
            oids = [c["oid"] for c in _json_list(pr.commit_titles) if c.get("oid")]
            if not oids or all(o in existing for o in oids):
                continue  # not a squash (originals already on main) → skip
            squash = session.get(Commit, pr.merge_commit_sha) if pr.merge_commit_sha else None
            file_bulk = bool(squash and squash.files_changed > large_commit_file_count)
            if not (file_bulk or len(oids) >= min_commits):       # balanced gate (§3.3)
                continue
            candidates.append((pr, [o for o in oids if o not in existing]))
        if not candidates:
            return
        # ONE batched fetch — only candidate refs, not refs/pull/* wildcard (§3.1):
        refspecs = [f"refs/pull/{pr.number}/head:refs/whygraph/pull/{pr.number}" for pr, _ in candidates]
        repo.fetch_refs(refspecs)                                 # NEW thin Repository method (4.5a)
        for pr, new_oids in candidates:
            for oid in new_oids:
                row = repo.commit_metadata(oid)                   # reuse Commit.from_git_log (commands.py:250-279)
                session.add(_to_commit_row(row, on_default_branch=0))
```

#### 4.5a `services/git/` — two thin additions
The enricher needs two small read/fetch helpers; both are new `ShellCommand` pairs in
`services/git/commands.py` + thin `Repository` methods (mirror `GitDiffCmd`/`Repository.diff`,
`commands.py:90-122` / `repository.py:146-188`):
- `fetch_refs(refspecs: list[str])` → `git fetch origin <refspec…>` (one process, many refspecs).
- `commit_metadata(oid)` → `git log -1 --pretty=… --shortstat <oid>` parsed by the **existing**
  `Commit.from_git_log` (`commands.py:250-279`); do not write a new parser.

#### 4.6 `scan/crawler.py` + `cli/commands/scan.py`
Wire the new phase under a `--pr-origins / --no-pr-origins` flag, **default on** (resolved Q1/default):
squash recovery is the point, so it runs unless opted out. Always **skipped under `--no-remote`** (the
fetch needs network — like the other remote phases). Mirror the existing `--codegraph/--no-codegraph`
flag wiring.

#### 4.7 `analyze/backfill.py` + `mcp/evidence.py:backfill_evidence_descriptions`
Origin commits are normal-sized real commits → reuse `backfill_all` / the whole-diff path
(`evidence.py:408-449`) as-is; `Repository.diff(commit)` now resolves because the object is local
(4.5 step 1). **No new descriptor code** — only ensure origin commits flow through the existing
`normal` branch (they will: `files_changed <= threshold` and `llm_description is None`).

### Phase 2 — per-line attribution

#### 4.8 `mcp/evidence.py` — new `_attribute_squash_origins` signal
Model on `_predecessor_blame` (line 299) and `_walk_past_boring` (line 244). For each blame hit SHA
that is the **`merge_commit_sha` of an *enriched* squash PR**, re-blame the same range at the PR's
`head_sha` and emit each resulting original commit as `source="pr-origin"`.

**Trigger condition (fixed in the consistency pass).** The trigger is **not** "the squash is a bulk
commit" — that would *skip* commit-rich-but-small squashes that the §3.3 gate enriched (file-bulk
**OR** commit-rich). The correct, gate-agnostic predicate is: *the blame SHA equals a PR's
`merge_commit_sha` and that PR has ≥1 origin `Commit` row* (i.e. it was actually enriched, so
`head_sha`'s objects are local). That naturally covers exactly the PRs Stage 1 enriched.
```python
def _attribute_squash_origins(repo, target, *, blame_shas, session) -> list[BlameHunk]:
    out: list[BlameHunk] = []
    for pr in _enriched_squash_prs_for(session, blame_shas):   # merge_commit_sha in blame_shas
                                                               # AND has on_default_branch=0 origins
        try:
            hunks = repo.blame(target.path, target.line_start, target.line_end, rev=pr.head_sha)
        except GitError:
            continue                                           # best-effort, mirrors line 321
        out.extend(h for h in hunks if not h.is_uncommitted)
    return out
```
Feed the result into the existing `labeled_hunks` list (line 194) tagged `"pr-origin"`; the dedupe /
priority / cap machinery (`_should_replace`, `_SOURCE_PRIORITY`, the `limit` slice) then needs no
further change. Each hunk resolves via the existing `session.get(Commit, sha)` (origin rows exist) and
`_linked_prs` (via `commit_titles`).

#### 4.9 Source-label plumbing (three small edits)
- `evidence.py:39-44` — add `"pr-origin": 0.5` to `_SOURCE_PRIORITY` (just below `blame`=0: a real
  authoring commit reached through the squash is high-precision).
- `rationale_generator.py:119-124` — add `"pr-origin": "original commit recovered from a squash-merged PR"`.
- `rationale.py:37-62` — extend the `source` docstring enum (doc-only).

#### 4.10 Gate main-walk-only queries on `on_default_branch` (defensive)
- `evidence.py:_boring_shas_in` (286-296) — add `.where(col(Commit.on_default_branch) == 1)`.
- `path_history.py:area_history_commits` (the `Commit`⨝`CommitFileChange` join at `path_history.py:116-121`)
  — same guard.

**Why "defensive":** origin commits get **no** `commit_file_change` rows (the enricher writes only
`Commit` rows, §4.5) and default `refactor_score=0`, so they are *already* naturally excluded from
both area-history (which joins through `commit_file_change`) and refactor-walk (which filters
`refactor_score >= BORING_THRESHOLD`). These guards make the invariant explicit and protect against a
future broad `select(Commit)` consumer; they are belt-and-suspenders, not load-bearing — which is why
acceptance criterion 3 (byte-identical area-history) holds trivially.

#### 4.11 `core/config.py` + `whygraph.example.toml` — new config (resolved Q4)
Two distinct config homes, by *which component consumes the value* (corrected in the consistency pass):

**(a) Rendering caps → `RationaleConfig` (`config.py:256`), `[rationale]` table.** Consumed by the
rationale generator (§4.2). Add three fields (mirror `RationaleConfig`'s existing field shape):
```python
    pr_roster_max_commits: int = 30        # max squashed-commit headlines rendered into a PR block
    pr_discussion_max_comments: int = 20   # max PR comments rendered into a PR block
    pr_comment_max_chars: int = 500        # per-comment body clip
```
```toml
[rationale]
# pr_roster_max_commits = 30        # squashed-commit headlines shown per PR in rationale evidence
# pr_discussion_max_comments = 20   # PR comments shown per PR in rationale evidence
# pr_comment_max_chars = 500        # each PR comment clipped to this length
```

**(b) Enrichment gate → `AnalyzeConfig` (`config.py:248-252`), `[analyze]` table.** Consumed by the
scan enricher (§4.5). Add one field next to `large_commit_file_count`:
```python
    pr_origin_min_commits: int = 5         # commit-rich half of the Stage-1 enrichment gate (§3.3)
```
```toml
[analyze]
# pr_origin_min_commits = 5         # enrich a squash PR's original commits once it collapsed >= this many
```
`pr_origin_min_commits` is the commit-count half of the balanced gate (§3.3); the file-bulk half
reuses the existing `large_commit_file_count`. **All four** new fields get the same `>= 1` validation
that `max_diff_chars`/`large_commit_file_count` already have (extend the validation pass at
`config.py:469-477`). Document them commented-out in `whygraph.example.toml` alongside the existing
`# max_diff_chars` / `# large_commit_file_count` lines under `[analyze]` (`whygraph.example.toml:23-28`)
and under the existing `[rationale]` table (`whygraph.example.toml:33`).

---

## 5. Schema / migration

One Alembic migration (`db/migrations/`) for a single additive change: the `commit.on_default_branch`
column (4.4). No new table (see 4.3). `commit_titles`/`comments` already exist — Phase 0 needs **no**
migration. Follow the existing `migrations/versions/` autogenerate flow; the column is additive with a
server default, so existing rows backfill to `on_default_branch=1` and re-scans are safe.

---

## 6. Acceptance criteria

1. **Stage 0:** `whygraph_evidence_for` on a line owned by a squash commit returns that commit's PR
   with non-empty `commit_titles` and `comments`; the rationale prompt shows a "Squashed commits"
   roster and "Discussion". No DB migration required for this criterion.
2. **Stage 1:** after `whygraph scan` on a repo with a squash-merged PR, the `commit` table contains
   the PR's original commits with `on_default_branch=0`, and `_linked_prs` resolves each back to the
   PR via `commit_titles` (no link table).
3. **Stage 1 isolation:** area-history and refactor-walk results are byte-identical to pre-change for
   a repo with no squash PRs (origin commits never leak into those paths).
4. **Per-line:** for a line in a squash commit, the evidence bundle contains a `source="pr-origin"`
   entry whose commit is the original feature-branch commit that authored that line (verified against
   `git blame <head_sha>`).
5. **Offline & `--no-remote`:** the MCP query path and `whygraph scan --no-remote` make **no** network
   calls; enrichment fetch happens only in the remote scan phase.
6. **Graceful degrade:** a non-GitHub repo, a GC'd/absent PR ref, or a squash-vs-head mismatch yields
   today's behaviour (PR-level evidence + bulk stub), never an error.

---

## 7. Testing plan

### Unit
1. `_pr_dict` includes decoded `commit_titles` + `comments`; malformed JSON → `[]` (via `_json_list`).
2. `_format_pr` renders roster + discussion; respects the caps; empty inputs add no lines.
2b. Cap config: the three new `RationaleConfig` fields default correctly, reject `< 1` (mirror
   `max_diff_chars` validation), and a lowered cap actually truncates the rendered roster/discussion
   (passed in via the generator, not read from a global).
3. `commit.on_default_branch` defaults to 1, origin insert sets 0; `_linked_prs` resolves an inserted
   origin commit to its PR via `commit_titles` (incl. a commit shared across two PRs → both returned).
4. Balanced gate: squash (oids-absent-from-`commit`) + file-bulk → enrich; squash + commit-count ≥
   `pr_origin_min_commits` → enrich; squash but below both thresholds → skip; merged-as-own-commits
   (oids present on main) → skip regardless of size.
5. `_boring_shas_in` / area-history exclude `on_default_branch=0` rows.
6. Source priority: a SHA surfacing as both `pr-origin` and `area` is kept as `pr-origin`.

### Integration (fixture repo with a real squash merge)
7. End-to-end scan → origin `commit` rows populated with `on_default_branch=0`; the targeted batched
   `git fetch` is mocked to a local bundle (assert it requests only candidate refspecs, not the
   wildcard) so the test is offline.
8. `whygraph_evidence_for` on a squash line returns a `pr-origin` entry matching `git blame <head_sha>`.
8b. Per-line trigger covers a **commit-rich but non-file-bulk** squash (≥ `pr_origin_min_commits`
   commits, ≤ `large_commit_file_count` files): it was enriched, so its line gets a `pr-origin` entry —
   guards against the §4.8 trigger regressing to a file-bulk-only check.

### Regression
9. Repo with **no** PRs / no remote: evidence + rationale output unchanged vs. `main`.
10. `tests/test_smoke.py` invariants intact (package imports; MCP server named `"whygraph"`).

---

## 8. Rollout order (single-shot recipe)

### Step 1 — Stage 0: surface `commit_titles` + `comments` (+ rendering-cap config)
Add the three **`RationaleConfig`** cap fields + `>= 1` validation + `[rationale]` example-toml lines
(4.11a); edit `_pr_dict` (4.1, uncapped) and `_format_pr` (4.2, caps passed in); thread the caps from
the generator into `_format_evidence`/`_format_pr`; add a local `_json_list` in the generator.

**Model:** Sonnet 4.6 · **Complexity:** Low
**Why this model:** Well-specified renderers reading columns already on the row + three config fields
that mirror existing siblings; no schema migration, no queries.
**Execution notes:** Caps live in **`RationaleConfig`** (`config.py:256`), **not** `AnalyzeConfig` —
the generator is built from `RationaleConfig` (§4.2). Store them on the generator in `from_config`/
`__init__` and pass into `_format_evidence(evidence, caps)` → `_format_pr(pr, caps)`; do **not** reach
into `get_config()` from the module-level formatters. Add a local `_json_list` (mirror `evidence.py:58`)
and reuse `_indent_block` (`rationale_generator.py:69`). Mirror `max_diff_chars`'s `>= 1` validation
(`config.py:469-477`). `_pr_dict` stays **uncapped** (§4.1). Do not touch the bulk-stub path. Do **not**
add `pr_origin_min_commits` here — that field belongs to Step 3 (the enricher). Ships independently.
**Verify:** `uv run pytest tests/ -k "pr_dict or format_pr or rationale_config"`; manual `whygraph_evidence_for` on a known squash line.

### Step 2 — Schema: `commit.on_default_branch` + migration
Add the column (4.4) to `db/models/commit.py`; autogenerate one additive Alembic migration (§5). No new
table — the PR↔commit link is the existing `commit_titles` (4.3).

**Model:** Sonnet 4.6 · **Complexity:** Low
**Why this model:** A single column add + an additive migration; nothing to design.
**Execution notes:** Column default uses `server_default=text("1")` (mirror `pull_request.py:28`). Run
the project's Alembic autogenerate flow; review the generated migration is additive-only (no table
rewrite). Do **not** add a join table or a `pr_id` column (§4.3 explains why).
**Verify:** `uv run alembic upgrade head` on a copy of a scanned DB; existing rows show `on_default_branch=1`.

### Step 3 — Enricher: fetch + persist original commits
Add `AnalyzeConfig.pr_origin_min_commits` + validation + `[analyze]` example-toml line (4.11b). Add the
two thin git helpers `fetch_refs` / `commit_metadata` (4.5a). Build `scan/pr_origin_enricher.py` (4.5),
wire the phase + `--pr-origins` flag (4.6), apply the §4.10 query guards.

**Model:** Opus 4.8 · **Complexity:** High
**Why this model:** New scan phase with network, idempotency, balanced-gate detection, and the
`on_default_branch` isolation contract — correctness across re-scans and `--no-remote` matters.
**Execution notes:** **One targeted batched** `git fetch` carrying only the gated candidates' refspecs
(§3.1) — **not** the `refs/pull/*` wildcard. Apply the balanced gate (§3.3) exactly as in the §4.5
skeleton: squash (oids-absent-from-`commit`) AND (file-bulk OR `len(commit_titles) >= pr_origin_min_commits`).
`pr_origin_min_commits` is added **here** (`AnalyzeConfig`), not in Step 1. Default the phase **on**; skip
entirely under `--no-remote`. Reuse `Commit.from_git_log` for full bodies via the new `commit_metadata`
helper (4.5a); do **not** invent a new commit parser. Apply the §4.10 guards in the same step or
area-history could regress for a future consumer.
**Verify:** integration test 7; unit test 4; regression test 9.

### Step 4 — Lazy diffs for origin commits
Confirm origin commits flow through the existing `normal` backfill branch (4.7).

**Model:** Haiku 4.5 · **Complexity:** Low
**Why this model:** Likely zero code — verifying the existing `backfill_all` path already covers them.
**Execution notes:** Origin commits are `files_changed <= threshold` with `llm_description is None`, so
`backfill_evidence_descriptions` (`evidence.py:408-412`) already routes them to `backfill_all`. Only add
code if a test proves they're missed. Do not special-case them.
**Verify:** integration test 8 shows a populated `llm_description` on a `pr-origin` commit.

### Step 5 — Per-line attribution signal + labels
Add `_attribute_squash_origins` (4.8) and the three label edits (4.9).

**Model:** Opus 4.8 · **Complexity:** High
**Why this model:** Novel evidence signal in the dedupe/priority machinery; correctness of the
`rev=head_sha` re-blame and best-effort error handling is the crux of the feature.
**Execution notes:** Model the function on `_predecessor_blame` (`evidence.py:299-324`) — same
`rev=`-blame call, same per-event `GitError` swallow. Feed its hunks into the existing `labeled_hunks`
list (line 194) with `source="pr-origin"`; the dedupe/priority/cap machinery then needs no change beyond
the `_SOURCE_PRIORITY` entry. Do not add a second blame implementation.
**Verify:** unit test 6; integration tests 8 & 8b; acceptance criteria 4 & 5.

---

## 9. Open Questions for the reviewer

**None outstanding** — all six review forks are resolved and folded into §0 (scope, attribution,
provider, link storage, `commit_titles` retention, Stage-0 caps, enrichment cadence, enrichment gate,
fetch strategy, enrichment default). The plan is ready for final approval to implement.

---

## 10. Summary

A squash merge doesn't destroy a feature's history — it relocates it to the provider, and WhyGraph
already ingests most of it (`commit_titles`, `comments`) then drops it at the last serialization step.
**Stage 0** stops dropping it (no schema, immediate win). **Stage 1** fetches the squash PR's original
commits once during the remote scan and stores them as `Commit` rows flagged `on_default_branch=0` —
linked back to their PR through the existing `commit_titles`/`_linked_prs` path (no new table) — so
they enrich evidence without polluting the main-walk queries. **Per-line
attribution** reuses the existing `blame(rev=…)` machinery — blaming the queried range at the PR's
`head_sha` lets git itself map each squashed line to its original commit, mirroring how
predecessor-blame already crosses renames. The expensive, low-fidelity alternative (LLM/graph diff
splitting) is explicitly deferred; every degraded path falls back to today's safe behaviour.
