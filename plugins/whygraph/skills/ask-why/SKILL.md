---
name: ask-why
description: When the user asks "why does X exist?", "what is X for?", "is X still needed?", "can I delete X?", or any similar intent-question about an existing code symbol, surface the /rationale slash command as the way to get a verbatim WhyGraph rationale (purpose, why, constraints, tradeoffs, risks). Read-only — do not run the command yourself.
---

# Suggest /rationale for intent questions

The user is asking about the *intent* behind existing code. WhyGraph has a structured rationale — purpose, why, constraints, tradeoffs, risks — for each symbol, derived from git history (and PRs/issues when the scan picked them up). The `/rationale` slash command prints it verbatim.

## What to do

1. Identify the symbol the user is asking about. Common shapes:
   - **File + name:** "why does `validate_session` in `auth/middleware.py` exist?"
   - **Just a name:** "what is `RoleResolver` for?"
   - **Indirect:** "is this still needed?" — then the symbol is whatever the user is currently looking at.
2. Suggest the command. Two forms are accepted:
   - `/rationale <qualified_name>` — e.g. `/rationale whygraph_rationale_brief`.
   - `/rationale <path>:<line_start>-<line_end>` — e.g. `/rationale src/whygraph/mcp_server.py:957-1056`. Use this when you have a precise range and the qualified_name is ambiguous or unknown.
3. Mention `--force` only if the user suspects the cached rationale is stale and wants to regenerate it.
4. **Do not run the command yourself.** Let the user invoke it — that's the explicit opt-in. Don't paraphrase what `/rationale` would return; the whole point is verbatim grounding.

## When NOT to suggest /rationale

- The user is adding *new* code with no predecessor — no rationale exists yet.
- The user is about to *edit* the symbol — the `pre-edit` skill handles that path automatically.
- The question is conceptual or domain-level rather than per-symbol ("how does authentication work?" — broader than one symbol).
- WhyGraph hasn't been scanned in this project. Suggest running `whygraph scan` first.

## What you should NOT do

- Don't pre-answer the rationale from your own reading of the code. The user is asking *because* they want WhyGraph's grounded answer, not your guess.
- Don't suggest `/rationale` for trivial cases ("why is this variable named `x`?"). It's for symbols with non-trivial history.
