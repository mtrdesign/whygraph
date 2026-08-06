# Concepts

WhyGraph has two core ideas: **evidence** (the raw history behind code) and **rationale** (an
LLM-synthesized explanation built from that evidence). Understand these two, and the rest of the tool
follows.

## Evidence

Evidence is the factual record: the commits that touched a chunk of code, the blame behind each line,
the pull requests that merged it, the issues those PRs closed, the **person** who wrote each commit,
and whether that commit is [shipped history or work in
progress](scanning.md#how-whygraph-sees-branches). WhyGraph collects it during [`scan`](scanning.md)
and links it together, so one lookup returns the whole chain.

You reach evidence two ways, because there are two questions to ask:

- **`whygraph_evidence_for`** - "which commits authored *these specific lines*?" Line-blame-driven and
  anchored to HEAD. Best when you have a precise range or a symbol.
- **`whygraph_area_history`** - "which commits ever touched *this file*, or anything that became this
  file?" It walks the rename chain, so it reaches code that's since been deleted, moved, or fully
  rewritten - commits that blame physically can't surface.

The two reinforce each other. Evidence keeps line-level precision; area history reaches further back.

## People

"Who wrote this?" only has a useful answer if one human is one row. In practice a single person
commits under several addresses - work laptop, personal machine, GitHub's web UI - and under more than
one display name, so a plain `GROUP BY author_email` reports the same person three times while looking
authoritative.

WhyGraph resolves identities during every scan, and the resolution is **evidence-driven, never
heuristic**. In order of authority:

1. **git's mailmap** - a human explicitly asserting "these contacts are me". The only signal that's a
   statement of intent rather than an inference, so it wins, and it also decides the display name.
2. **GitHub's own author triples** - login, name, and email asserted together by GitHub.
3. **The GitHub noreply address** - `12345+login@users.noreply.github.com` deterministically yields
   `login`. Pure string work, so it still resolves identities on a `--no-remote` scan.
4. **Byte-equal emails** - the same address is the same person by construction.

What it refuses to do matters as much. It never merges on display name, never fuzzy-matches
addresses, and never treats a shared or bot address as evidence, because **a false merge is worse than
a false split**. Silently collapsing two people attributes one person's work to another with no
symptom; leaving one person as two rows is merely untidy.

Contribution counts follow the same branch rule as everything else: only commits on the default branch
are counted, so a squash-merged PR and its recovered originals aren't double-counted.

## Rationale cards

A rationale card is WhyGraph's answer to "why does this exist?" It takes the evidence bundle, hands it
to the configured LLM, and gets back a structured card with exactly five fields:

- **purpose** - what this code is for.
- **why** - why it was written this way.
- **constraints** - what it must preserve.
- **tradeoffs** - what was given up, and for what.
- **risks** - what could break if you change it.

The card comes back with provenance too - `model`, `provider`, `cached_at` - and an `evidence_count`
summarizing how many commits, PRs, and issues fed it.

!!! note "Five fields, nothing more"
    A card carries those five narrative fields and nothing else - no extra score or rating. The
    rationale is the evidence-grounded explanation, full stop.

### Caching

Cards are **persistently cached**, keyed by content - the target, the evidence bundle, the provider,
and the model. Generate a card once and the next identical lookup is a sub-second database read. Change
the underlying code (so the evidence shifts) and the next call regenerates. You pay the LLM cost only
when something actually changed.

## The CodeGraph split

WhyGraph sits on top of CodeGraph and stays out of its lane:

| Layer | Owns |
|---|---|
| **CodeGraph** | "what's connected to what" - callers, callees, `find_symbols`, type hierarchy |
| **WhyGraph** | "why it exists and when it changed" - evidence, rationale, area history |

When you target a symbol by `qualified_name`, WhyGraph asks CodeGraph to resolve it to a file and line
range, then layers its own history on top.

**Its MCP surface exposes no graph-traversal tools** - three tools, all about history. To walk callers
and callees from your editor, install CodeGraph's own MCP server alongside. WhyGraph's other surfaces
do traverse the graph, because they read CodeGraph directly rather than over MCP: the
[Explorer](playground.md) draws it, and the [chat assistant](chat.md) can search symbols and query the
index. The narrow MCP surface is a deliberate boundary, not a limitation of the underlying data.
