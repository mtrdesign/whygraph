# Chat eval fixtures

Suites for `scripts/eval_tool_choice.py --suite`. Each file must be answerable
on its project's repository.

| File | Suite | Slug | Repository |
|---|---|---|---|
| `engineer.json` | `engineer` | `whygraph` | a clone of this repository |
| `lead.json` | `lead` | `biome` | biomejs/biome |
| `onboarding.json`, `product.json` | see file | see file | see file |
| `holdout/engineer.json` | `engineer-holdout` | `whygraph` | this repository |
| `holdout/lead.json` | `lead-holdout` | `biome` | biomejs/biome |

`judge_rubric.md` is the blind judge's rubric. `holdout/` is run only at a
gate; see its README.

Rule: a draft case produced by `scripts/chat_signals.py` is never committed
until a person has rewritten its question. Real user questions and rating
comments stay out of the repository.
