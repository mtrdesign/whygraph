---
name: WhyGraph Ask-Why
description: When the user asks "why does X exist?", "what is X for?", "is X still needed?", "can I delete X?", or any similar intent-question about an existing code symbol, call the whygraph_rationale_brief MCP tool and surface its output verbatim. Read-only — don't paraphrase or pre-answer.
---

# Answer intent questions with a verbatim WhyGraph rationale

The user is asking about the *intent* behind existing code. WhyGraph has a structured rationale — purpose, why, constraints, tradeoffs, risks — for each symbol, derived from git history (and PRs/issues when the scan picked them up). Call the `whygraph_rationale_brief` MCP tool and surface the result, rather than guessing from the code itself.

## What to do

1. Identify the symbol the user is asking about. Common shapes:
   - **File + name:** "why does `validate_session` in `auth/middleware.py` exist?"
   - **Just a name:** "what is `RoleResolver` for?"
   - **Indirect:** "is this still needed?" — then the symbol is whatever the user is currently looking at.

2. Call `whygraph_rationale_brief` with one of:
   - `qualified_name` — e.g. `auth.session.RoleResolver`
   - `path` + `line_start` + `line_end` — use this when you have a precise range and the qualified_name is ambiguous or unknown

3. Present the response **verbatim**, especially `purpose`, `why`, `constraints[]`, `tradeoffs[]`, and `risks[]`. The value here is the grounded answer; don't paraphrase or layer your own interpretation on top.

4. If the user suspects the cached rationale is stale, re-call with `force_refresh=True`.

## When NOT to call `whygraph_rationale_brief`

- The user is adding *new* code with no predecessor — no rationale exists yet.
- The user is about to *edit* the symbol — the WhyGraph Pre-Edit Brief instructions already cover that path; you'll fetch the brief there.
- The question is conceptual or domain-level rather than per-symbol ("how does authentication work?" — broader than one symbol).
- WhyGraph hasn't been scanned in this project. Surface the tool's error verbatim and suggest running `whygraph scan` first.

## What you should NOT do

- Don't pre-answer the rationale from your own reading of the code. The user is asking *because* they want WhyGraph's grounded answer, not your guess.
- Don't call the tool for trivial cases ("why is this variable named `x`?"). It's for symbols with non-trivial history.
