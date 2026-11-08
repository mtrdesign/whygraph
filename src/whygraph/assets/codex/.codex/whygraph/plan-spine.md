# Plan spine

Read this file once before you write a plan. It says what a good plan holds. The plan format is in your own instructions.

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

## Problems before solutions

- A Blocker is a constraint that the change puts at risk. Quote the constraint verbatim.
- A Risk is any other problem. Give each Risk one mitigation.
- State the problems before the steps that depend on them.

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

## Acceptance

- Make item 1 the full test command of the project, as the `Verify candidates` report gives it.
- Add the user-visible outcomes of the task as the next items. Add at most six items in all.
- Give each item a **Check**: a literal command, or `manual: <what to look at>`.

## Self-review checklist

Check each box before you hand the plan over.

- [ ] Each file reference is real and has a line number.
- [ ] The plan names what it copies and what needs no change.
- [ ] Non-goals are present.
- [ ] Blockers and Risks are not mixed.
- [ ] Each step has a Verify and a Model.
- [ ] Acceptance item 1 is the full test command.
- [ ] Each question has options and a default.
- [ ] No em dash or en dash appears. Use a plain hyphen.
- [ ] The writing rules hold in each `Change` line.
