---
name: pre-edit
description: Fetches WhyGraph rationale — intent, constraints, tradeoffs, and risks — for existing code before it is changed, so an edit respects the original intent instead of rediscovering it.
when_to_use: Before editing, refactoring, renaming, moving, deleting, or replacing any existing function, class, or symbol; and when answering "why does this exist?", "what is this for?", "is this still needed?", "can I delete this?", or "what breaks if I change this?".
user-invocable: false
allowed-tools: mcp__whygraph__whygraph_rationale_brief, mcp__whygraph__whygraph_evidence_for
---

# WhyGraph Pre-Edit Rationale

WhyGraph stores a structured rationale card per code chunk — purpose, why it exists,
constraints, tradeoffs, risks — derived from git history (and PRs/issues when scanned).
Pull the card with `whygraph_rationale_brief` before changing existing code so the edit
respects the original intent.

## When to call `whygraph_rationale_brief`

**Call before** editing a function/method/class body; refactoring, renaming, moving,
deleting, or replacing a symbol; or answering "why does this exist? / is this still
needed? / can I delete this?". Anything more than a typo, comment, or formatting fix.

**Skip when** adding brand-new code; trivial edits (typos, comments, whitespace, import
sort, formatter output); you already fetched the card for the same symbol this
conversation and the code hasn't changed; or the user told you to skip it.

## How to call it

Identify the chunk one of two ways (pass one, not both):

- **By location** (preferred): `path` + `line_start` + `line_end`.
- **By name**: `qualified_name` (e.g. `auth.session.refresh_token`) — CodeGraph resolves it.

Leave other args at their defaults unless you have a specific reason (`force_refresh=True`
only when the code has clearly changed since the last card).

## How to use the card

- **Constraints** are non-negotiable — preserve them. If the user's request requires
  breaking one, surface that explicitly *before* making the change.
- **Tradeoffs** explain why an obvious-looking improvement may already have been considered
  and rejected. Weigh them before "fixing" something.
- **Risks** are flagged to the user *before* the change, not after.
- **`evidence_count`**: if it's empty or low, the card has little git history behind it —
  treat it as a weak signal and verify against the code yourself.

If the call returns `isError: true`: no scan DB → surface verbatim (user runs `whygraph
scan` first); calling by `qualified_name` failed (no CodeGraph DB, or stale graph) → retry
by `path` + `line_start` + `line_end`. Either way, proceed with the edit using your own
judgment after flagging the cause.

## When to also call `whygraph_evidence_for`

Use this read-only companion (same `path/lines` or `qualified_name` args; returns commits +
linked PRs/issues) only when the card looks wrong and you want the source commits, the user
asks for a symbol's *history*, or the card looks thin and you want to ground-check a claim.
Don't call it routinely — the card already summarises the evidence.

## What you should NOT do

- Don't dump the full card to the user unless asked. Use it to inform *your* edit, then
  mention only what affects the change ("preserving the X workaround from the rewrite").
- Don't call the tool repeatedly for the same chunk within one task.
- Don't treat the card as authoritative if the code clearly diverges from it — flag the
  divergence to the user.
