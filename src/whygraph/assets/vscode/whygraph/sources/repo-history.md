---
name: repo-history
description: Whether similar changes happened before in this repo, and what they teach.
requires: whygraph_area_history
tools: read, whygraph_area_history, whygraph_rationale_brief
section: Prior art
enabled: true
budget: 20
consent: false
---

*Question:* have similar changes happened before in this repo, and what can we learn from them?

1. For each distinct file in the working set, call `whygraph_area_history(path=...)` to surface the commits and PRs that have ever touched it - including changes to code that has since been moved, renamed, or deleted, which line-blame alone cannot reach.
2. Filter aggressively. Skim each hit's narrative; ignore unrelated history. Surface 3-5 hits at most.
3. For each kept hit, call `whygraph_evidence_for` on the past change's range and read the commit / PR narrative. Call `whygraph_rationale_brief` on at most 2 ranges whose evidence is rich (>= 3 commits); use `cached_only=True` on any other range you want a card for. Read `whygraph://pr/{n}` for the 3-5 kept hits instead of relying on inlined comments.
4. Report concrete takeaways per hit: *"PR #X did Y → side-effect Z; constraint they added: ..."* - not generic platitudes.

Report one line per kept hit: `- PR #N (<path>) - <takeaway>`.
