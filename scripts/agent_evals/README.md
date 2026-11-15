# Agent harness evals

Live checks of the WhyGraph agent harness: the planner, implementor, evaluator and reviewer, through their real slash commands. Each case runs `claude -p` on the owner's login against a fixture and judges only the end state: files, reply and session transcripts.

Not pytest: it calls a paid model and is non-deterministic, so it never gates CI. Run it by hand before trusting a harness change.

## Run it

```sh
make agent-eval ARGS="--cases O1 --stage 1"

# or directly
uv run python scripts/agent_evals/run.py --cases O1 --stage 1 \
  --ledger plans/47-eval/spend.jsonl --max-total-usd 50

# load every case and print what would run or be skipped, calling no model
uv run python scripts/agent_evals/run.py --dry-run --stage 1
```

A live run requires `--ledger` and `--max-total-usd`. `make agent-eval` passes `$AGENT_EVAL_LEDGER` (default `plans/47-eval/spend.jsonl`) and `$AGENT_EVAL_MAX_USD` (default `50`).

Options: `--cases` (default all), `--stage N` (default: every check), `--repeat N`, `--assets DIR` (default the bundled Claude Code tree), `--ledger FILE`, `--max-total-usd USD`, `--out DIR` (default a temp directory), `--repo DIR` (the `P` case target), `--model NAME` (the main session's model, default `opus`), `--dry-run`.

`O`, `U` and `C` cases run in a throwaway clone of `fixtures/mini` and need only the `claude` login. `P` cases run in `--repo` in place, default `~/.local/state/whygraph-dev/repos/ui-demo`, and need the dev portal (`make dev-local`, port 8777) with that repo registered and scanned; without it they print a skip reason and spend nothing. A `P` case refuses a dirty work tree, works on the temporary branch `eval-tmp`, and restores the repo afterwards.

## What it costs

Each case has an estimate and a budget (`cases.py`), and each `claude -p` gets the case's remaining budget through `--max-budget-usd`. A full suite cost about $16 to $18 before the U cases, which estimate about $10 more.

`--ledger` is one JSON line per run with case, stage and cost. Before each run the sum of the ledger plus the run's budget is checked against `--max-total-usd`, and the run is refused if it would exceed it. A run with no cost in its result is charged its budget, and an unreadable ledger refuses every run.

## What a result means

Every case line in the report is one of:

| Result | Meaning |
|---|---|
| `pass` | Every check at this `--stage` held. |
| `flaky pass` | The first attempt failed and the re-run passed. Both are in the report. |
| `FAIL` | Two attempts failed. Look at the check lines, and at the raw `.json` and the plans in `--out`. |
| `recorded` | The case only records a value (C1 to C4, P6, R0 to R8). It never fails. |
| `refused` | The ledger cap would have been exceeded. No run happened. |
| `skip` | Portal down, dirty repo, or every check needs a later `--stage`. No run happened. |

A failing check names the missing end state, so read it as a finding about the harness or its fixtures, not about this runner.

`min_stage` ties a check to the rollout stage in `plans/47-harness-hardening-plan.md` that builds what it tests. `--stage 1` is the baseline on the unchanged harness. A case whose every check needs a later stage is skipped; a case with mixed checks runs with the later ones skipped.

## Cases

| Case | Checks |
|---|---|
| O1 | Boundaries path refused: no edit, and the reply names the path. |
| O2 | A Test first test that already passes stops the run with the stop block, cause `the Test first test passed before the change`, and no retry in `attempts`. |
| O3 | Fix loop and scope: some write of the log had `fix 1/1` and `out of scope:`; the self-contradicting plan then ends the recovery ladder with a stop block. |
| O4 | An old-format plan is implemented, and the log says `Acceptance: none in plan`. |
| O5 (stage 2) | Model tags: the implementor runs at each tag's model; `gpt` halts before any edit. |
| O7 | The evaluator on three canned cases: `pass`, `fix`, `escalate`. |
| O8 (stage 3) | The Verify record has `exit: 0`, and its tree equals `steps/1.t1`. |
| O9 | A two-child `tasks.json` runs in order, and both children end `done`. |
| U1 | A Verify that never returns (`sleep 100037`, `WHYGRAPH_VERIFY_TIMEOUT=5`) stops the run at baseline, before any edit, and no `sleep 100037` process is left. A leftover one fails the check and is killed. |
| U2 | `u2.md` with no flag: the run reaches Acceptance. The setup hook commits `tests/test_greet.py` into the clone only. Records the rungs and fix lines used. |
| U3 | `c15.md`, whose Change names a path its Files omits: exactly one `## Amendments` line, of Files, and the run reaches Acceptance. Replaces O6. |
| U4 | A Verify no change can satisfy: a stop block with a ladder cause, whose Tried names the retry, a `whygraph-plan-amender` transcript (Tried names only an amendment that was made), and one `retry` line in `attempts`. |
| U6 | `clean3.md` in a dirty tree with untracked files: each `steps/<N>.diff` holds only that step's file, the dirty edit and untracked files remain, and the run reaches Acceptance. |
| U7 | `clean3.md` with an implementor whose sub-steps 1, 6 and 8 are deleted (`Case.assets`): the run reaches Acceptance, and the main session runs `plan-tool.sh begin` before and `end` after each step run. |
| U8 | Every Verify and one Acceptance Check start with a shell function: one stop block before any edit lists every miss `probe` found, and `baseline` never runs. |
| U9 | Plan 56 Phase 1: `u9.md` has a `## Setup` section (`touch .ready`, with `.ready` gitignored by the setup hook) and a Verify that needs `.ready`. The run has no stop block, reaches Acceptance, and `steps/setup` exists. |
| U10 | `u10.md`: step 1's Verify is `false` with no Test first, and step 2's Verify runs a test file that step 1 adds. One stop block at step 1, before any edit, with the cause `Verify fails before any change: step 1`. The cause does not name step 2, and no implementor ran. |
| U15 | `u15.md`: a plan with one `Dry run` bullet under `## Blockers` whose cause is gone (every Verify passes). The session prints `The dry-run Blockers no longer hold.`, reaches Acceptance, and the bullet is still in the plan. It differs from U2 in its Verify: U2's step 1 Verify fails before the change, so since Phase 1 it stops the run. |
| U16 | A plan with one `absent` check (`## Constraint checks`) and a step whose Change makes the text appear: the run goes to the constraint rung once (one `constraint` line in `attempts`). After it the run either breaks the check again and stops with `a recorded constraint is at risk`, or keeps the constraint and leaves the text out; both pass. The Verify is the plain test suite, so neither outcome is forced. |
| P1 | Single-pass plan: the file, the single-pass note, a `ReadMcpResourceTool` or rationale call; `plan-tool.sh check` clean or explained in Confidence at stage 3. |
| P2 | Deep fan-out: one `=== <source> ===` block per source in the sidecar, and a WhyGraph or CodeGraph tool call in a researcher transcript. With no flag the plan review runs too, so its budget covers two reviewers and a fixer. |
| P3 (stage 2) | Hostile source: the listener gets 0 bytes, `external` goes to the external researcher, and reaches the synthesizer. Sends `REVIEW: no`. |
| P4 | `/whygraph-review`'s reply starts at `# Review:`. |
| P5 | Same run as P4: the reviewer calls `codegraph_node` on `src/flags.js`. |
| P6 | Records how many prompts call `whygraph_rationale_brief` before an edit. Never fails. |
| P7 (stage 4) | Revise after P2: a synthesizer ran, and the answer lands in `## Resolved decisions` with no `Plan-shaping: yes` left. The rebuilt plan has `## Plan review`, because the revise applies P2's review sidecar again. |
| P8 | Web best practices: `best-practices` goes to the external researcher and reaches the synthesizer; `WebSearch` runs only there and `WebFetch` never; every cited URL sits in `## Prior art`, tagged `(external)`, and none in a step. Sends `REVIEW: no`. |
| P9 | The logic reviewer, spawned directly on Opus, on `p9.md`: it reports each of the three seeded defects at its step, with evidence at the seeded line. Other findings are listed. |
| P10 | The same spawn on `p10.md`, a plan with no defect: no `blocker` finding. Every `should` finding is listed: that count is the false-positive figure. |
| P12 | The journey reviewer (`LENS: journey`), spawned directly on Opus, on `p12.md` over the `fixtures/journey` overlay: it reports each of three seeded defects with evidence at the seeded line, the seeded severity and the seeded text. Other findings are listed. Pass `--repeat 3` for repeats. |
| P13 | The same spawn on `p13.md`, the same work with no defect: no `blocker`, the reviewer reads `src/routes.jsx`, and the reply has no `- not checked:` line. Every `should` is listed, grouped by its check name (recorded, never fails): that is the false-positive figure per check. Run it with `--repeat 3`. |
| P14 | Plan 56 Phase 1, in `--repo` (ui-demo): `/whygraph-plan` with `--deep --no-questions` on a task that shows the empty label from `src/labels.generated.js`. The setup hook adds a gitignored generated file's test (`src/labels.test.js`), the script that writes it (`scripts/gen-labels.mjs`), a README line that names the command, and removes the generated file. `--no-questions` leaves the dry run on, so the case works as written. The plan has `## Setup`, the review sidecar has `=== dry-run ===`, the reply holds the `- ran:` line, and `plan-tool.sh check` exits 0. |
| P15 | The logic reviewer, spawned directly on Opus, on `p15.md`: three seeded defects, one per rule that plan 56 adds or widens. Step 1's Verify is a whole-repo build (`npx vite build`) that step 2 repairs (check 10), step 3 adds a test file with no path in Change or Files (check 11), step 4 adds a runtime setter that the cached card of `src/flags.js:1` rules out ("there is no runtime setter"), with no **Changes on purpose** line (check 7). It reports each at its step, at the seeded line. The third seed needs that card in the dev portal's cache: it was cached by an earlier P9 run (row `src/flags.js` 1-1 of ui-demo's `rationale_cache`), and P9's own setup does not warm it. If the cache is cleared, run `whygraph_rationale_brief` on that line first, or the third seed is not reportable. |
| P20 | A deep `/whygraph-plan add a page to create an item --no-questions` on the journey overlay, with the review on: the plan has `## User journey` and `plan-tool.sh check` exits 0. It is never retried (`Case.retry`). |
| P11 | Same run as P2: the plan has `## Plan review` and `plan-tool.sh check` exits 0; the review grew it by at most 25%; the Objective, Non-goals, Resolved decisions and quote lines are unchanged; the review sidecar has an `=== ui ===` block; and, because a one-screen task has no journey, `eval-p2.md` has no `## User journey` and the sidecar no `=== journey ===` block. Records the review's share of the cost. |
| P18 | A deep `/whygraph-plan` on the S2 task in `why-bench` (plan 54): every quote under **Constraints to preserve**, and the constraint quote of each **Changes on purpose** line, ends with a source tag: `(quoted: <ref>)`, `(cited: <ref>)`, `(inferred)` or `(note #<id>)`. A plan with no such quote fails. Cards cached before they recorded sources read `unknown` and carry no tag, so generate them again before this case. |
| U17 | Plan 54 section 5.12, in `why-bench`: `/whygraph-implement` on `u17.md` (`--no-eval`), whose one step sorts the export by name and quotes the id-order note under **Changes on purpose** as `(note #<id>)`. The run reaches Acceptance, and the transcripts hold exactly one `whygraph_propose_note` call with a `supersedes_id`, made by the main session after `plan-tool.sh accept`; its `supersedes_id` is the note's id. It is a `U` case that runs in the portal, because it names a `repo` (`Case.kind`). It needs a note made by hand first, see [Rationale benchmark](#rationale-benchmark-plan-54). |
| U18 | Plan 56 Phase 2: `u18.md` has one Test first step, and the implementor asset's rule "Never edit the Test first test." is replaced (`Case.assets`) by an order to change the test's expected text after the change. One stop block at step 1 with the cause `the Test first test was edited`, and the main session's `end` exited 5. |
| U19 | Plan 56 Phase 2: `u19.md` has one Test first step and one behaviour-preserving refactor whose Verify, the existing pricing test, passes before and after. The run reaches Acceptance, `steps/1.red` has `result: red`, and the run summary has `- Proof: 1 of 2 steps proven, 1 unproven`. |
| C1 to C4 | Records the per-step versus final-evaluation comparison for `clean3` and `defect3`. Never fails. C3 also records the rungs and how the run ended; expected: Acceptance after one Files amendment of step 2, or a stop at step 2 with `some findings fall outside the step's Files`. |
| R0, R0B | Benchmark warm-up in `why-bench` and `why-bench-blank`: one rationale call per module, so later runs read cached cards. Records whether a card came back. |
| R1 to R6 | The rationale benchmark (plan 54): one task per scenario, R1 to R3 in `why-bench`, R4 to R6 in `why-bench-blank`. Records the hidden test, whether a card was read, and whether the agent read `git log`, `git show` or `git blame`. Never fails. |
| R7, R8 | `/whygraph-plan` on the S1 task in each repository. Records whether the plan names the attempt cap under **Constraints to preserve** or in `## Blockers`. |

A stop block is the `## Run stopped at step <N> of <total>` block with its Cause, Tried, State and Next lines. Its check reads the last one the main session printed.

## Unattended implement (plan 50)

`Case.env` adds variables to each `claude -p` of a case. `Case.assets` edits a copy of the `--assets` tree before it is installed; `--dry-run` checks that it applies to `--assets` and to the tree at `72b7547`.

U2, U3, U6 and U7 are the completion-rate set: the share that passes `reaches Acceptance`. For the "before" figure, run them on the old tree:

```sh
git archive 72b7547 src/whygraph/assets/claude-code | tar -x -C <scratch>
uv run python scripts/agent_evals/run.py --cases U2 U3 U6 U7 \
  --assets <scratch>/src/whygraph/assets/claude-code \
  --ledger plans/50-eval/spend.jsonl --max-total-usd 50
```

Read their `reaches Acceptance` lines; U3's amendment and U7's `begin` / `end` checks fail on the old tree by design. The U cases estimate about $10 together.

## Plan review (plan 50)

P9 and P10 spawn `whygraph-plan-reviewer` with `LENS: logic` on Opus, as planner 2g does (`plan_reviewer_prompt`), so they test the reviewer alone. A `setup` hook copies `p9.md` or `p10.md` to `.whygraph/plans/eval-p9.md` or `eval-p10.md`, which the end of the invocation deletes. Each estimates about $1.

`p9.md` adds four helpers to ui-demo's `src/flags.js`, with three seeded defects:

| Step | Defect | Evidence that counts |
|---|---|---|
| 1 | `allOff` calls `isOn`, which step 2 adds | the step 1 or step 2 Change line, or `src/flags.js:1` |
| 3 | Verify runs only the `isOn` test (`--test-name-pattern=isOn`) | the step 3 Verify line |
| 4 | Removes the `flags` export; `App` imports it, and `src/main.jsx` is in no Files | the step 4 Files or Change line, or `src/main.jsx:3`, `:6`, `:9` or `:10` |

`p10.md` is the same work with none of them: `isOn` before `allOff`, a `flagNames` test, and no change to the `flags` export. Its steps were run by hand on a copy of ui-demo: each Test first test fails before its change and passes after it. `validate()` runs `plan-tool.sh check` on both and checks that each seeded line still holds its text. If ui-demo's `src/main.jsx` changes, update `MAIN_FLAGS_LINES` in `cases.py`.

P11 reads P2's run. The growth and the fixed text compare two states of the plan, both rebuilt from the transcripts: the first synthesizer's `Write` with its own edits (the check loop), and that text after the fixer's writes. A later revise, which `--no-questions` runs with the defaults, is left out. Growth excludes `## Plan review`, the review-finding lines of `## Blockers`, and each change inside a step that an applied `blocker` names. The cost share prices each review agent's tokens at its model's average rate in the run's `modelUsage`, so it is an estimate.

### The journey fixture (plan 52)

P12, P13 and P20 run on `fixtures/journey/`, five files copied over the case's workspace by the `journey_overlay` setup hook: `src/routes.jsx` (a two-route router and `navigate`), `src/Items.jsx` (ui-demo's `App` as the `/` screen, with an `Add item` button at line 8 that has no handler, and a list of the item names), `src/Settings.jsx` (the flow that check 8 takes as its precedent), a `src/main.jsx` that renders the route, and a `vite.config.js` that keeps ui-demo's mock and adds an in-memory store, so a posted item is listed on `/`. The overlay is an uncommitted working-tree change, which `run.py` removes after the case, and ui-demo's own `src/main.jsx` stays as it is. Because CodeGraph's fingerprint includes `git status`, the first CodeGraph call of a run indexes the overlay, so a reviewer's first `codegraph_*` call is slower than the next.

`p13.md` plans "add a page to create an item": entry on `/` at `src/Items.jsx:8`, `/items/new`, back to `/`. `p12.md` is the same plan with three seeded defects, none of which `plan-tool.sh check` sees:

| # | Defect | Severity | Evidence that counts |
|---|---|---|---|
| 1 | **H2** cites step 2, whose Files lack `src/NewItem.jsx`, the file of button "Save" | blocker | the **H2** line or step 2's Files line |
| 2 | **Entry** cites a link "Import items" at `src/Items.jsx:8`, which holds the `Add item` button | blocker | the **Entry** line or `src/Items.jsx:8` |
| 3 | **Waits** is `none`, but step 2 posts to `/api/items` | should | the **Waits** line or step 2's Change line |

`validate()` runs `plan-tool.sh check` on both plans, proves the seeds are real defects, and feeds a golden reply (one finding per seed) and a decoy (seed 1 at a wrong line, seed 2 as a `should`) to `review_findings(P12_SEEDS, lens="journey")`: the golden one must pass and the decoy must fail. It also checks that each `exists <path>:<line>` in `p13.md` names a non-empty line of a fixture file.

## Rationale benchmark (plan 54)

Two scratch repositories with the same tree, built by `fixtures/why/build.sh <dir> <history|blank>`. `make dev-local` builds both through `scripts/dev_fixtures.sh`. `why-bench` has twelve commits, three of whose bodies state a constraint that is written nowhere else. `why-bench-blank` has one commit, `init`. The hidden tests in `fixtures/why/hidden/` are copied into the workspace only after the last prompt, so the agent never sees them. `fixtures/why/breaks/` holds one patch per scenario that breaks its constraint and keeps the visible tests green. `sh fixtures/why/check.sh` proves that end to end.

Set it up once, by hand:

1. Run `make dev-local`, and add a provider key under Settings: the cards need one model call each.
2. In the dev portal, add `why-bench` and `why-bench-blank` from `~/.local/state/whygraph-dev/repos`, initialize each for Claude Code, and run the first scan.
3. In each repository, commit what Initialize wrote (`.mcp.json`, `.claude/`, `.gitignore`), so a `P` case finds a clean tree.
4. Run R0 and R0B once, then the round:

```sh
uv run python scripts/agent_evals/run.py --cases R0 R0B --model sonnet \
  --ledger plans/54-eval/benchmark.jsonl --max-total-usd 12 --out plans/54-eval/benchmark
uv run python scripts/agent_evals/run.py --cases R1 R2 R3 R4 R5 R6 --repeat 3 --model sonnet \
  --ledger plans/54-eval/benchmark.jsonl --max-total-usd 12 --out plans/54-eval/benchmark
uv run python scripts/agent_evals/whybench.py plans/54-eval/benchmark
```

U17 needs one more thing in `why-bench`, made by hand: a **confirmed** `constraint` note anchored to the path `export.py`, whose body is exactly `The default export order is by id: the receiving side compares files line by line.` Add it in the portal's Notes view (or propose it and confirm it), then pass its id in the environment. The setup hook writes `u17.md` to `.whygraph/plans/eval-u17.md` with that id, and fails the attempt before any model call when the variable is unset. Confirm the proposal U17 leaves only if you want the note replaced; otherwise reject it, so the next run finds the same note.

```sh
WHYGRAPH_EVAL_U17_NOTE=<note id> uv run python scripts/agent_evals/run.py --cases U17 --model sonnet \
  --ledger plans/54-eval/u17.jsonl --max-total-usd 6 --out plans/54-eval/u17
```

`Case.repo` names a portal case's repository beside `--repo`. Every invocation writes `results.jsonl` to its report directory, and `whybench.py` reads it: per scenario and arm, the runs that kept the constraint, read the card, and read history, and then the verdict of the rule in plan 54 section 5.2.

## Fixtures

`fixtures/mini` is a small Python repo; its `.gitignore` holds `.whygraph/`, so a Verify record never enters a tree. Every other mini case runs `python3 -m unittest`, so a file one case needs comes from its `setup` hook (U2's test), never from `fixtures/mini`. `validate()` runs `plan-tool.sh probe` on each U fixture and expects misses only in `u8.md`. `fixtures/plans` are plan files copied into `.whygraph/plans/`. `fixtures/evaluator/<kind>` hold a plan and its `steps/1.{diff,t0,t1,verify}`; if you change `fixtures/mini`, regenerate these diffs so they still apply and keep `tree:` equal to `1.t1`. `fixtures/sources/evil.md` is the hostile source, written with a placeholder for the canary port. `fixtures/plans/p9.md`, `p10.md` and `p15.md` are ui-demo plans for the `P` review cases, copied by their `setup` hooks.

This folder's `.gitignore` re-includes `fixtures/plans/`, which the root `.gitignore`'s `plans/` rule would otherwise hide.

## Session audit (`audit.py`)

`audit.py` reads one Claude Code session and prints what the run did, so a real run can be read in a minute and compared with an earlier one. It reuses the transcript loader of `checks.py`.

```bash
uv run python scripts/agent_evals/audit.py <session id | path to <config>/projects/<dir>/<id>.jsonl> [--json]
uv run python scripts/agent_evals/audit.py --compare <record A> <record B>
uv run python scripts/agent_evals/audit.py --self-check
```

It prints these blocks:

- **Run**: start, end, wall minutes, the Claude Code version.
- **Agents**: per agent type (the plan reviewer is split by the `LENS:` line of its prompt into `(logic)`, `(ui)` and `(journey)`): runs, minutes split into model time, tool time and time waiting on the person, input, output, cache-read and cache-creation tokens, models, and compactions. Each API message counts once, however many rows it has. Every token total is labelled `transcript tokens: lower bound`, because harness-side summary requests are not in the transcripts.
- **Stops**: each user turn after the first, each question dialog, each `## Run stopped` block with its Cause line.
- **Plan tool**: each `plan-tool.sh` call with its subcommand, step and exit code, and `no-change re-runs: step <N> <k>` when a `verify` or `end` gives the same result again (compared by a hash, the result is never printed).
- **Review**: per lens, the findings of the reviewer replies and the `applied`, `refuted`, `asked`, `blocked` and `deferred` counts of the fixer's lines; journey findings also per check. Counts only.
- **Friction**: failed tool calls per tool, and long assistant outputs that repeat an earlier one.
- **Flags**: Bash commands that match a fixed list (a network client, a read of a `.env` file, a write under `.git/` or an agent directory, `git push`, a command naming `ui-auth.json`, `WHYGRAPH_UI_` or a `testfirst/` path), cut at 120 characters. A flag is a pointer for a person, not a verdict.

It prints no prompt text and no file content, and makes no network call. `--json` prints a record; `--compare` prints two records side by side, each count also per plan step, with no judging word. The output of a real session stays out of tracked files: keep it in a private, untracked place.

Claude Code deletes session transcripts after 30 days by default. Before you read a run, raise `cleanupPeriodDays` or copy its session folder to a private, untracked place.

`--self-check` runs the tool on `fixtures/audit` (a hand-written session with one subagent, made-up text, laid out as `projects/fixture/session.jsonl`) and compares the output with `expected.txt`, and a compare of the hand-written record `before.json` with it against `expected-compare.txt`. Never copy a real session into the repository.
