---
name: whygraph-rationale
description: Which constraints and risks from the recorded rationale this change endangers.
requires: whygraph_rationale_brief
tools: read, whygraph_rationale_brief, whygraph_evidence_for, codegraph_node
section: Constraints / risks cited verbatim
enabled: true
budget: 20
consent: false
---

*Question:* which constraints and risks from the rationale does this change endanger?

1. For each working-set node, read the `constraints` and `risks` the planner inlined in your prompt (verbatim from its card). Call `whygraph_rationale_brief` only for a symbol outside the working set. Quote verbatim.
2. For each constraint, judge whether the user's task is likely to violate it. Quote the constraint verbatim; state the conflict in one line.
3. For each risk, prioritise ones the change is likely to trigger. Quote verbatim.
4. Treat thinly evidenced cards (only a commit or two behind them) as hints, not directives - note explicitly when you cite them.
5. If a working-set node has `purpose: (no rationale recorded)`, do **not** invent constraints for it. Note "no rationale recorded" and move on.

A card with `partial: true` was built while some commits had no description yet: treat it as usable but thinner, and say so when you cite it.

For a symbol outside the working set, call `whygraph_rationale_brief` with `cached_only=True`. If a call errors with `code: budget_exceeded` or `generation_not_permitted`, do not retry; use `whygraph_evidence_for` and say so under Limitations.
