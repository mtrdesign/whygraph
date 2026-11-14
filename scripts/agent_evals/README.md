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
| U16 | A plan with one `absent` check (`## Constraint checks`) and a step whose Change makes the text appear: the run goes to the constraint rung once (one `constraint` line in `attempts`), then stops with `a recorded constraint is at risk`. |
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
| P11 | Same run as P2: the plan has `## Plan review` and `plan-tool.sh check` exits 0; the review grew it by at most 25%; the Objective, Non-goals, Resolved decisions and quote lines are unchanged; the review sidecar has an `=== ui ===` block. Records the review's share of the cost. |
| P18 | A deep `/whygraph-plan` on the S2 task in `why-bench` (plan 54): every quote under **Constraints to preserve**, and the constraint quote of each **Changes on purpose** line, ends with a source tag: `(quoted: <ref>)`, `(cited: <ref>)`, `(inferred)` or `(note #<id>)`. A plan with no such quote fails. Cards cached before they recorded sources read `unknown` and carry no tag, so generate them again before this case. |
| U17 | Plan 54 section 5.12, in `why-bench`: `/whygraph-implement` on `u17.md` (`--no-eval`), whose one step sorts the export by name and quotes the id-order note under **Changes on purpose** as `(note #<id>)`. The run reaches Acceptance, and the transcripts hold exactly one `whygraph_propose_note` call with a `supersedes_id`, made by the main session after `plan-tool.sh accept`; its `supersedes_id` is the note's id. It is a `U` case that runs in the portal, because it names a `repo` (`Case.kind`). It needs a note made by hand first, see [Rationale benchmark](#rationale-benchmark-plan-54). |
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

`fixtures/mini` is a small Python repo; its `.gitignore` holds `.whygraph/`, so a Verify record never enters a tree. Every other mini case runs `python3 -m unittest`, so a file one case needs comes from its `setup` hook (U2's test), never from `fixtures/mini`. `validate()` runs `plan-tool.sh probe` on each U fixture and expects misses only in `u8.md`. `fixtures/plans` are plan files copied into `.whygraph/plans/`. `fixtures/evaluator/<kind>` hold a plan and its `steps/1.{diff,t0,t1,verify}`; if you change `fixtures/mini`, regenerate these diffs so they still apply and keep `tree:` equal to `1.t1`. `fixtures/sources/evil.md` is the hostile source, written with a placeholder for the canary port. `fixtures/plans/p9.md` and `p10.md` are ui-demo plans for the `P` review cases, copied by their `setup` hooks.

This folder's `.gitignore` re-includes `fixtures/plans/`, which the root `.gitignore`'s `plans/` rule would otherwise hide.
