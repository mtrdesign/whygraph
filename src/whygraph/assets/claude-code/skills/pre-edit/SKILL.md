---
name: pre-edit
description: Before editing, refactoring, renaming, deleting, or replacing existing code, fetch its WhyGraph rationale to learn the intent, constraints, tradeoffs, and risks behind it. Also use when answering "why does this exist?" / "is this still needed?" questions about a symbol.
---

# WhyGraph Pre-Edit Brief

WhyGraph stores a structured rationale for each code chunk in this project — purpose, why it exists, constraints to preserve, tradeoffs, and risks — derived from git history (and PRs/issues when the scan picked them up). Before changing existing code, pull this brief so the edit respects the original intent rather than rediscovering it from scratch.

## When to call `whygraph_rationale_brief`

**Call before:**
- Editing the body of an existing function, method, or class
- Refactoring, renaming, moving, deleting, or replacing a symbol
- Answering "why does this exist?", "what is this for?", "is this still needed?", "can I delete this?"
- Any change you would describe as more than a typo, comment, or formatting fix

**Skip when:**
- Adding entirely new code with no predecessor
- Trivial edits: typos, comment-only changes, whitespace, import sorting, formatter output
- You already fetched the brief for the same symbol earlier in this conversation and the underlying code hasn't changed
- The user has explicitly told you to skip the brief

## How to call it

The tool accepts two ways to identify the chunk — pass one, not both:

- **By location** (preferred when you have it): `path` + `line_start` + `line_end`. Most accurate, no name resolution needed.
- **By name**: `qualified_name` (e.g. `auth.session.refresh_token`). CodeGraph resolves it to a file/line range.

Other useful args:

- `force_refresh=True` — bypass the rationale cache (use when the underlying code has clearly changed since the last brief).
- `model` and `timeout_sec` — leave at defaults unless you have a reason.
- `min_score_pct` — leave at default (0.5); raises evidence harshness if increased.

## How to use the result

The tool returns:

- `purpose` — one-line summary of what the code does
- `why` — the historical / contextual rationale
- `constraints[]` — things that must be preserved
- `tradeoffs[]` — design tradeoffs visible in the history
- `risks[]` — risks of modification
- `confidence` — 0 to 0.85 (the ceiling lifts when refactor-lineage detection lands)
- `evidence_count` — `{commits, prs, issues}` summarising how much history backed the card
- `cached` — `True` if served from the rationale cache

Apply it like this:

- **Constraints** are non-negotiable. Preserve them. If the user's request requires breaking one, surface that explicitly before making the change.
- **Tradeoffs** explain why an obvious-looking improvement may have already been considered and rejected. Weigh them before "fixing" something.
- **Risks** are flagged to the user *before* the change, not after.
- **Low confidence (< 0.4)**: treat the brief as a hint, not a directive — verify against the code itself.
- **High confidence (≥ 0.7)**: weight it heavily; it's well-supported by history.
- **Empty `evidence_count`**: the chunk has no meaningful git history yet (e.g. brand-new code). The brief will be thin — proceed with your own judgment.

If the tool returns `isError: true`, the cause is one of:
- **No scan DB in this project** — surface the error verbatim; the user needs to run `whygraph scan` first.
- **No CodeGraph DB** when calling by `qualified_name` — fall back to calling by `path` + `line_start` + `line_end`.
- **Symbol not found** when calling by `qualified_name` — likely a stale graph; try the path+lines form instead.

In every case, proceed with the edit using your own judgment after flagging the cause to the user.

## When to also call `whygraph_evidence_for`

Use this read-only companion tool when:

- The brief looks wrong and you want to inspect the source commits directly
- The user asks for the *history* of a symbol, not just its rationale
- Confidence is low and you want to ground-check a specific claim against raw evidence

It takes the same `path/line_start/line_end` or `qualified_name` arguments and returns `{target, evidence}` with commits + linked PRs/issues. Don't call it routinely — the brief already summarises the evidence.

## What you should NOT do

- Don't dump the full brief verbatim to the user unless asked. Use it to inform *your* edit, then mention only the parts that affect the change ("Note: this function previously had a workaround for X — preserving that behavior in the rewrite.").
- Don't call the tool repeatedly for the same chunk within one task.
- Don't treat the brief as authoritative if the code clearly diverges from what it describes — flag the divergence to the user.
