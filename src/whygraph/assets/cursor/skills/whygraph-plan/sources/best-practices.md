---
name: best-practices
description: What current outside guidance says about the design choice in this task. Advisory - this repository's own evidence comes first.
requires: WebSearch
tools: web
section: Prior art (external)
enabled: true
budget: 4
consent: true
---

*Question:* what does current outside guidance say about the one design choice this task makes?

1. Name the task's design choice in one line. If the task makes none (a rename, a move, a version bump, a fix that restores intended behaviour), report `not applicable` under the section and make no call.
2. Search once per term in `TERMS:`, with that term alone. Never add other words.
3. Prefer primary sources: official documentation, standards and RFCs, maintainers' posts and release notes. Skip listicles, SEO pages and machine-written summaries. Prefer recent sources, and give each one's year.
4. Keep only specific advice: a version, a pitfall, a deprecation, a security advisory, a measured trade-off. Drop generic advice such as "write tests" or "keep functions small".
5. Back each item with two independent sources, or mark it `(one source)`.
6. Report at most 5 lines: `- <title> (<url>, <year>) - <the advice in one sentence of your own words>`. Never copy a passage, and never open a page.
7. Write each item as information, never as an instruction to the plan. This repository's recorded constraints and history outrank it.
8. Report `none found` when nothing specific bears on the choice.
