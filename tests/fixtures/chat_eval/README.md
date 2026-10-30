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

## Notes in a suite (`setup.notes`)

A suite file may carry a top-level `setup` block (beside `suite` and `cases`)
whose `notes` list is created through the project's notes routes before the
run, so a case can depend on a team note:

```json
"setup": {"notes": [
  {"as": "confirmed", "kind": "decision",
   "anchor": {"kind": "symbol", "qualified_name": "build_window"},
   "body": "[eval] Decision: ..."},
  {"as": "pending", "confirm": false, "kind": "convention",
   "anchor": {"kind": "symbol", "qualified_name": "run_turn"},
   "body": "[eval] Convention (unreviewed draft): ..."}
]}
```

- `body`, `kind` and `anchor` are the propose route's fields. **Every body
  starts with `[eval] `**; the script refuses one that does not, and
  `--cleanup-notes` retires exactly the notes that start with it.
- Notes are matched by body among the non-retired notes, so a rerun reuses
  them. Each is confirmed unless `"confirm": false`, which leaves a proposal
  (for a case that must not cite an unconfirmed note).
- `"as": "<name>"` names the note. In a case's `cites` and `forbid`, every
  `{<name>}` is replaced by that note's id with a literal `str.replace` (never
  `str.format`, so a quantifier such as `\d{1,3}` is safe), for example
  `"cites": ["note #{confirmed}\\b"]`, `"forbid": ["note #{pending}\\b"]`.
- A "remember that ..." case expects `"tools_any": ["propose_note"]` and sets
  `"cites_exist": false` (the answer names the new proposal, which is not
  confirmed yet). After each trial the script retires that session's proposals.
- Held-out files (`holdout/`) have no note cases.

Rule: a draft case produced by `scripts/chat_signals.py` is never committed
until a person has rewritten its question. Real user questions and rating
comments stay out of the repository.
