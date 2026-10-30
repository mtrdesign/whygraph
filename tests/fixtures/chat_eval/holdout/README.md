# Held-out chat eval set

Twelve cases (6 on `whygraph`, 6 on `biome`) that no tuning step may see.

- Run only at a gate: `--suite tests/fixtures/chat_eval/holdout/engineer.json`
  and `--suite tests/fixtures/chat_eval/holdout/lead.json`.
- Never read this folder while changing prompts, tools or harness code, and
  never use a result from it to tune. A case that leaks into a prompt, a tool
  description or an example stops being held out and must be replaced.
- Suite names are `engineer-holdout` and `lead-holdout`, so report rows and
  `--suite-k` keys never collide with the main suites.
- No question here appears in any suite one level up.
