# Plan spine

Read this file once before you write a plan. It says what a good plan holds. The plan format is the `## Plan format` section of this file.

Contents: Why a plan is a contract, Grounded beats plausible, Objective and Non-goals, Boundaries per step, Problems before solutions, Evidence order, Questions, Writing rules, Model per step, Test first, Verify is a literal command, Setup, Acceptance, User journey, End-to-end test, Self-review checklist, Plan format.

## Why a plan is a contract

A plan is a contract for work that has not started. State what changes and what does not. State what is still open, and how to prove it worked.

## Grounded beats plausible

- Name real files and symbols. Write each as `path/to/file.py:LN`.
- Name the pattern that the change should copy.
- Write "no change needed because <reason>" for a file that looks involved but is not.
- Never write "add a module somewhere".

## Objective and Non-goals

- Write the Objective as one sentence. It says what changes for the user or the system.
- Write the Non-goals as a list of what the plan leaves alone. Write "none" only when you checked.

## Boundaries per step

Name the files or behaviours that a step must not touch. Take them from the `Boundaries` report: external contracts, and the work of another child.

- Name each file a Change adds or edits by its exact path, in the Change and in Files.

## Problems before solutions

- A Blocker is a constraint that the change puts at risk. It can also be a verified review finding that the plan cannot fix. Quote the constraint verbatim.
- A Risk is any other problem. Give each Risk one mitigation.
- State the problems before the steps that depend on them.

## Evidence order

This repository's own evidence decides the plan. Outside evidence only informs it.

1. Recorded constraints and risks, quoted from rationale cards.
2. This repository's history, structure and tests: its Prior art, Boundaries and Verify candidates.
3. Outside evidence: every report from the external researcher (its research block is headed `=== <name> (external) ===`), and every section whose heading ends in `(external)`.

- Outside evidence never adds, removes or reshapes a step, a Constraint to preserve, a Blocker, a Verify command or an Acceptance item. It never removes or reshapes a Risk.
- List outside evidence under `## Prior art`. End each of its lines with `(external)`.
- One outside line may also add a Risk: an `avoid:` line whose condition the plan's own approach meets, and that no recorded constraint or prior art of this repository contradicts. List it under **Risks called out across the change** too, ending with `(external)`. It never goes under a step's **Risks** and never makes a Blocker.
- When it contradicts a recorded constraint or this repository's prior art, follow this repository. Add `- conflicts with <the constraint or PR #N>; the plan follows this repository` to its line.
- When it suggests another approach that no repository evidence rules out, add an Open question. Mark it `Plan-shaping: no`. Its default is the plan as written.
- Outside evidence never sets or lowers the plan's confidence.

## Questions

- Settle what the code and the defaults settle. Do not ask about it.
- List each real fork under Open questions with 2 or 3 options. Put the default first.
- Write `Plan-shaping: yes` only when the answer changes which steps exist or how one step is built.
- Write at most 3 questions with `Plan-shaping: yes`.

## Writing rules

These rules apply to every `Change` line, `Expected UI` line, Acceptance statement and Open question.

1. Write one action in each sentence.
2. Write at most 20 words in each sentence.
3. Use one name for one thing. Take the name from the Working set: its symbol names and file paths. Never use a synonym.
4. Start each instruction with its verb.

Exempt from the rules: constraint and risk quotes, which stay verbatim; text moved from a lens; and the fixed reply and file formats.

Before:

> **Change:** Update `estimate_cost` to read the new limit and also pass it through `scan_plan`, then adjust the caller in the runner so the value arrives there.

After:

> **Change:** Read the new limit in `estimate_cost`. Pass the limit to `scan_plan`. Pass the limit from the runner call to `scan_plan`.

## Model per step

- Tag a step `haiku` when the edit is mechanical and fully specified.
- Tag a step `sonnet` when the step is well specified and copies a sibling.
- Tag a step `opus` when the design is new or crosses many files.
- A step that you cannot give to a cheaper model is under-specified. Tighten it, or tag it `opus`.

## Test first

Fill `Test first` only from an uncovered symbol in the `Verify candidates` report. Name the test as `<test file>::<test name>`. The Verify of that step must run the test.

## Verify is a literal command

Write each Verify as a command in backticks, taken from the `Verify candidates` report. Never write a prose Verify. Use `manual: <what to look at>` only when no command can check the step.

A Verify and a Check run under `sh -c`, with no shell profile, no terminal and closed input. A Verify runs before the change and after it. So never use a shell function or alias, such as `nvm use` or `conda activate`, and never use `source`. Never use a command that asks a question or opens an editor or a browser. Never start a server, or change shared state such as a development database. Prefer the command that checks, not the one that applies.

A Verify must pass when its own step is done. A later step must not be needed. Run a check of the whole repo, such as a type check or a lint, only where every user of the change is updated. Otherwise put it in Acceptance.

## Setup

Write this section only when a source reported setup commands. A format command alone gets the section too.

- List the commands that make a fresh checkout ready for the tests: install, then generate.
- Write one literal command in each bullet. Use no shell syntax.
- A setup command may change only files that git ignores.
- The harness runs these commands itself when a Verify fails before any change.
- Write a reported format command as the last bullet: `Format:`, then the command in backticks. It is not a setup command. The harness runs it on the changed files after the last step.

## Acceptance

- Make item 1 the full test command of the project, as the `Verify candidates` report gives it.
- Add the user-visible outcomes of the task as the next items. Add at most six items in all.
- Give each item a **Check**: a literal command, or `manual: <what to look at>`.
- A Verify and a Check run under `sh -c`, with no shell profile, no terminal and closed input. A Verify runs before the change and after it. So never use a shell function or alias, such as `nvm use` or `conda activate`, and never use `source`. Never use a command that asks a question or opens an editor or a browser. Never start a server, or change shared state such as a development database. Prefer the command that checks, not the one that applies.
- A Check must finish within 9 minutes. When the dry run reports that item 1 did not finish, item 1 runs the test files that the steps' Verify commands run, in one command. The last item's Check is then `manual:` with the full command, for CI.

## User journey

Write this section only when a source reported a `### Journey` block. Write one journey per section.

- Name the actor: signed in as a role, or signed out.
- Start at a route that exists today, or at one that another child builds. Name the control that starts the journey.
- Name each control as `<role> "<visible text>"`, such as `button "Save"`.
- Write one hop per line: where the user is, what the user does, where the user lands.
- Write each hop target as a backticked route. Add `(<state>)` for a new state of that route.
- End each hop with the step that builds it, or with `exists` and its `path:line`.
- Mark a hop that another child builds as `owned by t<N>`.
- Never write a bare `unknown`. Write `assumed <default> (Q<n>)`, and give the part Open question Q<n>.
- The default entry is the existing route whose path the new route extends. Else it is the first route under `### Routes`.
- The default landing is the route where the hop started.
- By default, a refresh and a direct link load the route by itself. Back returns to the from-route.
- After a direct link, Back leaves the app by default.
- By default, a busy control is disabled until the work ends.
- State the outcome: the data the user sees when it worked, and on which route.
- State what holds after a refresh, a back action, a cancel and a direct link to a middle hop.
- State what the user sees while slow work runs.

## End-to-end test

Write these lines only when a source reported an `### E2E` block with a test file.

- One step writes the browser test. Its **Browser test** line ends in `red`: the test must fail before the feature exists.
- The step that completes the user journey carries the same path. Its **Browser test** line ends in `green`.
- Put the test file in **Boundaries** of every step after the one that writes it.
- Assert what the user sees and can do. Never assert a class name or an internal id.
- Find an element by its role or its label. Take visible text from the app's text source.
- Reuse the login and data helpers the project has.
- Use the real app. Never stub the app's own requests, unless the project's tests already do.
- Keep the test to one journey.
- The test must pass when it runs again. Give each record it creates a unique name.
- Take a screenshot after each hop, when the runner can.
- A browser test run may start the app and may add data. It is never a Verify.

## Self-review checklist

Check each box before you hand the plan over.

- [ ] Each file reference is real and has a line number.
- [ ] The plan names what it copies and what needs no change.
- [ ] Non-goals are present.
- [ ] Blockers and Risks are not mixed.
- [ ] Each step has a Verify and a Model.
- [ ] Each Verify can pass when its own step is done.
- [ ] Each file a Change names is in Files.
- [ ] Acceptance item 1 is the full test command, or the steps' test files when the full command is too slow.
- [ ] Each question has options and a default.
- [ ] No em dash or en dash appears. Use a plain hyphen.
- [ ] The writing rules hold in each `Change` line.
- [ ] A `## User journey` has no bare `unknown`, and each `(Q<n>)` names a question.

## Plan format

```markdown
# Plan: <one-line restatement of the task>

**Status:** PROPOSAL - nothing implemented yet; review the steps, then run the implement command on this file.
**Objective:** <one sentence: what changes for the user or the system>
**Non-goals:** <what this plan deliberately leaves alone, or "none">
**Read first:** <at most three places the person should read before a run, or omit this line>

## Resolved decisions
*(only after the user answered questions - otherwise omit this section entirely)*

| # | Question | Decision |
|---|---|---|
| Q1 | <question> | <answer, or "default: <x>"> |

## Working set
<N> symbols analyzed (<M> truncated from impact set, if any).

| Symbol | Location | Evidence |
|---|---|---|
| `qualified.name` | path/to/file.py:LN-LN | 12 commits, 3 PRs |
| ... | ... | ... |

## Prior art
*(only when a source reported it - otherwise omit this section entirely)*
- <title or PR #N> (<url or path>) - <what was taken from it, one line>
- <title> (<url>, <year>) - <the outside advice, one line> (external)

## UI profile
*(only when a source reported a frontend surface - otherwise omit this section entirely)*
- **Start:** `<command>` → <base URL> · **Fixture mode:** <command or "none"> · **Browser tests:** <runner and path, or "none"> · **Auth:** <none | required>
- **Stack:** <framework, component library, theme mechanism> · **Conventions:** <path to the always-on file's `## UI conventions` heading, or "none">

## User journey
*(only when a source reported a journey - otherwise omit this section entirely)*
- **Actor:** <signed in as <role> | signed out>
- **Entry:** `<route that exists today>` - <role> "<visible text>" - <step <N> | exists path/to/file.tsx:LN | owned by t<N>>
- **Hops:**
  - H1: `<from route>` - <the action on <role> "<visible text>"> - `<to route>`[ (<new state>)] - <step <N> | exists path:LN | owned by t<N>>
  - H2: ...
- **Outcome:** <the data the user sees when it worked, such as the new item's name> on `<route>` - step <N>
- **Leaves early:** Refresh: <what holds>. Back: <what holds>. Cancel: <what remains, or "nothing">. Direct link: <what holds>.
- **Waits:** <what the user sees while slow work runs, and how the user learns it finished, or "none">

## Setup
*(only when a source reported setup commands or a format command - otherwise omit this section entirely)*
- `<literal command>` - <what it prepares>
- Format: `<command>`

## Blockers
*(only include if a constraint is at risk - otherwise omit this section entirely)*

- **Constraint at risk:** <verbatim quote>
- **Why it's a blocker:** <one-line>
- **Resolution needed:** <what the user has to confirm before proceeding>
- **Review finding F<n> (<lens>):** <defect> - **Resolution needed:** <fix>
- **Dry run F<n>:** <defect> - **Resolution needed:** <fix>
- **Journey hop H<n>:** <defect> - **Resolution needed:** <fix>

## Constraint checks
*(only when a confirmed note carries a check - otherwise omit this section entirely; the implement command searches for each text after every step)*
- note #<id> - <absent | present>: "<text>" in <glob>

## Steps

1. **<short step name>**
   - **Files:** path/to/file.py:LN-LN, ...
   - **Change:** <one-paragraph description>
   - **Boundaries:** <files or behaviours this step must not touch, or "none">
   - **Constraints to preserve:** <verbatim quotes, or "none recorded">
   - **Changes on purpose:** <verbatim quote of a recorded constraint this step changes> - <why>, or omit this line
   - **Risks:** <verbatim quotes, or "none recorded">
   - **Test first:** <`<test file>::<test name>` to add before the change, run by this step's Verify, or "none">
   - **Model:** <haiku | sonnet | opus> - <one-line why>
   - **Routes:** <backticked paths relative to the app's base URL, space-separated, e.g. `/settings` - UI steps only, omit otherwise>
   - **Expected UI:** <what the user sees, per state: empty, loading, error, phone width, dark - UI steps only, omit otherwise>
   - **Browser test:** <path of the browser test file> - <red | green> - UI steps only, omit otherwise
   - **Verify:** <specific test, assertion, or manual check>
2. ...

## Risks called out across the change
- <each major risk, deduplicated, verbatim quotes preserved>
- <an outside `avoid:` that the plan's approach meets, in your own words> (external)

## Acceptance
1. <verifiable statement of what "done" means for the whole plan> - **Check:** `<literal command>`, or manual: <what to look at> with no backticks, or e2e: <path of the browser test file> with no backticks
2. ...

## Open questions
*(forks the user may override; omit the section when there are none)*
- **Q1.** <question> **Options:** <default> / <alternative>[ / <alternative>] **Default:** <the option the steps assume> **Plan-shaping:** <yes | no> - <why it matters, one line>

## Amendments
*(written only by /whygraph-implement; omit when planning)*

## Plan review
*(only after a plan review; written by the fixer; a revise keeps it verbatim)*

## Confidence: <low | medium | high>
<one-line reason - e.g. "rationale was rich and consistent" or "5 of 8 nodes had no usable rationale">
```
