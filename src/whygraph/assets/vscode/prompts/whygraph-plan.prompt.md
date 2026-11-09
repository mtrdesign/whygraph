---
description: Plan a non-trivial code change with WhyGraph rationale + CodeGraph symbols. Runs as the planner - one researcher per knowledge source, then a synthesizer - splits a large task into child plans only when needed, and asks the few plan-shaping questions after research.
name: whygraph-plan
argument-hint: <task description | tasks.json> [--shallow|--deep] [--split|--no-split] [--sources <a,b>] [--no-questions] [--refresh-profile] [--out <path>]
agent: whygraph-planner
---

# /whygraph-plan - plan a non-trivial change with grounded rationale

Plan a non-trivial code change with WhyGraph rationale + CodeGraph symbols. This prompt runs **as** the `whygraph-planner` custom agent (`.github/agents/planner.agent.md`) in the main chat. The planner researches the change with one researcher per knowledge source and a synthesizer, splits a large task into child plans only when needed, and returns the few plan-shaping questions after research.

You are in the main chat, so you are also the one who asks the user. Wherever your planner instructions say to reply `CLARIFY:` or a `QUESTIONS:` block and let the slash command ask, follow this prompt instead: ask in chat, wait for the answer, and continue in this conversation.

You have no terminal here. Find files with the read and search tools. The edit tool creates a file's parent directory, so no directory needs creating first. Leave `created` out of a `tasks.json`.

**Argument shape:** `<task description | tasks.json> [--shallow|--deep] [--split|--no-split] [--sources <a,b>] [--no-questions] [--refresh-profile] [--out <path>]`

## Step 1 - Parse arguments

Strip and remember these flags if present (any order, anywhere in the user's input):

- `--shallow` -> force single-pass mode (no fan-out).
- `--deep` -> force fan-out mode (skip the heuristic).
- `--split` -> force a split into child plans along the task's deliverables.
- `--no-split` -> forbid a split.
- `--sources <a,b>` -> run only these sources, by `name`, comma-separated (consume the next token). A named source runs even when its file says `enabled: false`. Each name must match `^[a-z0-9-]+$`.
- `--no-questions` -> ask nothing: every consent is `no`, and open questions take their defaults.
- `--refresh-profile` -> derive the UI profile (`.whygraph/profiles/ui.md`) again, even when the cached one is fresh.
- `--out <path>` -> save the plan to `<path>` instead of the default `.whygraph/plans/<slug>.md`. The token after `--out` is the path, not part of the task description.

Whatever remains is the **task description**. If it is empty, respond with:
`Usage: /whygraph-plan <task description | tasks.json> [--shallow|--deep] [--split|--no-split] [--sources <a,b>] [--no-questions] [--refresh-profile] [--out <path>]` and stop.

- If both `--shallow` and `--deep` are passed, respond with `Pass --shallow OR --deep, not both.` and stop.
- If both `--split` and `--no-split` are passed, respond with `Pass --split OR --no-split, not both.` and stop.
- If a `--sources` name does not match the pattern, respond with `Unknown source name: <name>` and stop.

When the task description is one path that ends in `.tasks.json`, this run plans one child of an earlier split: resolve `SOURCES_DIR` and `SPINE_PATH` (Step 2), then go to **Step 6 - Plan a child**.

## Step 2 - Resolve the paths

The repo root is the workspace root. Resolve these absolute paths before planning:

- `OUT_PATH` - you write the plan there.
  - If `--out <path>` was passed, use that path. Resolve a relative path against the workspace root. If the path does not end in `.md`, append `.md`.
  - Otherwise, slugify the task description: lowercase it, replace each run of non-alphanumeric characters with one `-`, strip leading and trailing `-`, and truncate to 60 characters. The default path is `<workspace root>/.whygraph/plans/<slug>.md`. If that file exists, append `-2`, `-3`, ... until the name is unused. Never overwrite.
- `SOURCES_DIR` - `<workspace root>/.github/whygraph/sources/`.
- `SPINE_PATH` - `<workspace root>/.github/whygraph/plan-spine.md`.
- `CAPTURE_PATH` - `.github/whygraph/capture.mjs`. It stays relative to the workspace root.

If the file at `SPINE_PATH` does not exist, respond with `The planning files are missing from .github/whygraph/. Open the WhyGraph portal, run Settings > Update agent files, then re-run /whygraph-plan.` and stop.

## Step 3 - Consent (before Phase 0)

A source file is repo content, so this prompt decides which sources need the owner's OK, before your Phase 0 runs. A source file can force the question; it can never suppress it.

With `--no-questions`, ask nothing: record `<name>=no` for every source that needs consent (below), and go to Step 4.

1. List `SOURCES_DIR/*.md`. Skip `README.md`.
2. Read each file's frontmatter only (its first 15 lines). Never read a source's body: only the researcher that runs it reads the body.
3. Keep a source when both hold:
   - It would run: with `--sources`, its `name` is in the list; otherwise its `enabled` is not `false`. `--shallow` changes nothing here, because the single-pass planner runs a consented source through one researcher.
   - Every tool in its `requires` is callable in this session. A bare name matches a session tool whose name equals it, or ends in `__<name>` or `/<name>`. An empty `requires` always matches. The bundled `medium.md` needs the Medium tools, which this agent does not have, so it is never kept here.
4. A kept source needs consent when any of these holds:
   - (a) Its `requires` or `tools` names an MCP tool other than the `whygraph_*` and `codegraph_*` tools. The word `read` is not an MCP tool.
   - (b) Its file name is not one of the six bundled sources: `code-structure.md`, `whygraph-rationale.md`, `repo-tests.md`, `repo-history.md`, `medium.md`, `ui-surface.md`.
   - (c) It says `consent: true`.
5. If no kept source needs consent, record `CONSENT: none` and go to Step 4.
6. Derive `TERMS` once: 1 to 3 technical topics from the task text, separated by `; `. Never use a project, client, company, product, file or symbol name in a term.
7. Ask the user in chat and wait: put all consent questions in **one** message, at most 4 consent questions. A fifth or later source is `no`; print `<name>: not asked (at most 4 consent questions per run); it will not run.` for each. Number the questions. Each question:
   - names the source (its `name`);
   - says what the source reaches: "an external service through <its non-WhyGraph, non-CodeGraph tools>" for (a), and "added to this repo, not shipped with WhyGraph" plus its `description` for (b);
   - shows the terms: `Search terms: <TERMS>`;
   - offers `No (default)` first, then `Yes`.
8. Record one line per asked source: `CONSENT: <name>=yes` only when the reply clearly says yes to that question, otherwise `CONSENT: <name>=no`. A question the reply leaves unanswered is `no`.

## Step 4 - Plan

Your planner input is:

```
TASK: <task description>
MODE: <shallow | deep | auto>
SPLIT: <yes | no | auto>
SOURCES_DIR: <absolute path>
SPINE_PATH: <absolute path>
SOURCES: <all | comma-separated names>
CONSENT: <name>=<yes | no>
(one CONSENT line per source that needed consent, or "CONSENT: none")
TERMS: <topic>; <topic>
(only when some CONSENT is yes - otherwise omit the line)
PARENT: none
OUT_PATH: <absolute path resolved in Step 2>
CAPTURE_PATH: .github/whygraph/capture.mjs
REFRESH_PROFILE: <yes | no>
```

- `MODE` is `shallow` for `--shallow`, `deep` for `--deep`, otherwise `auto`.
- `SPLIT` is `yes` for `--split`, `no` for `--no-split`, otherwise `auto`.
- `SOURCES` is the `--sources` list, otherwise `all`.
- `REFRESH_PROFILE` is `yes` for `--refresh-profile`, otherwise `no`.

Now follow your planner instructions with this input, from Phase 0. Do not restate them here.

## Step 5 - Clarify, split or questions

Print each reply your planner instructions produce **verbatim**, with the one exception in Step 7. Then act on its shape:

- CodeGraph is not available -> print the message and stop. Do not attempt a fallback or save anything.
- **Clarify.** Phase 1a found no seed symbol and you wrote no plan; your reply would be `CLARIFY: <one question> | options: <a>; <b>[; <c>]`.
  - With `--no-questions`, print `Re-run /whygraph-plan with a more specific task, or without --no-questions.` and stop.
  - Otherwise ask the user the question with its options in chat and wait. Then run Phase 1a again with the line `CLARIFICATION: <answer>` added to your input. Ask a clarifying question once per plan; when the seeds still do not resolve, reply as your instructions say and stop.
- **Split.** You wrote a `tasks.json` and no plan; your reply is `SPLIT: <path>`, then one line per child: `t<N> (wave <W>): <title>`. Nested delegation is unavailable for child plans here. Print the reply, then print `Run /whygraph-plan <path of the tasks.json> once per child, in a new chat each time. Each run plans the next child with PARENT: set.` and stop.
- **Questions.** You wrote a plan; your reply ends with a `QUESTIONS:` block. It is either `QUESTIONS: none`, or `QUESTIONS:` followed by lines of this shape:

  ```
  Q<n>. <question> | options: <default>; <alt>[; <alt>] | default: <default> | why: <one line>
  ```

  - `QUESTIONS: none`, or no block -> nothing to ask. You are done.
  - With `--no-questions` -> do not ask. Run your Phase 3 (revise) at once with `ANSWERS: defaults`.
  - Otherwise ask the user in chat and wait: put the questions in **one** message, at most 3 questions. If the block has more, ask the first 3; the rest take their defaults. For each question, show `Q<n>.`, then `<question>`, then `<why>`; list the options from `| options: ` in order, so the default is first, and label the default `<default> (default)`; say that the user may type their own answer instead, and that a question left unanswered takes its default.

**Revise inline.** After the answers, run your Phase 3 (revise) in this conversation, with this input added to the one you planned with:

```
MODE: revise
PLAN_PATH: <the plan's absolute path - the OUT_PATH you wrote>
REFRESH_PROFILE: no
ANSWERS:
- Q<n>: <the chosen option, without "(default)", or the user's own text>
```

(or `ANSWERS: defaults` with `--no-questions`). Print the revise reply verbatim. A revise reply always ends `QUESTIONS: none`; never ask again for the same plan.

## Step 6 - Plan a child

The task description is the path of a `tasks.json` that an earlier run wrote. Plan its next child.

1. Resolve the path against the workspace root. Validate the file. On a failed check, respond with `Refusing <path>: <reason>.` and stop.
   - Its path resolves to `<workspace root>/.whygraph/plans/<stem>.tasks.json`.
   - Read it. It parses as JSON and has `version: 1`.
   - `children` has 1 to 6 entries. Each `id` is one of `t1`-`t6`, and no `id` repeats.
   - Every `plan` matches `^\.whygraph/plans/[a-z0-9-]+-t[1-6]\.md$`.
   - Every `depends_on` entry names an existing `id`, and the dependencies have no cycle.
   This prompt has no terminal, so it cannot ask git whether the file is tracked; the implementor refuses a tracked `tasks.json` before it implements a child.
2. Pick the child: the first one, in ascending `wave` and then `id` order, that has no file at its `plan` yet. When every child has a plan, print `Every child of <path> has a plan. Run /whygraph-implement <path>.` and stop.
3. For `t1` only, run Step 3 (consent). Every other child gets `CONSENT: none` and no `TERMS` line. `REFRESH_PROFILE` is as in Step 4 for `t1` only; every other child gets `REFRESH_PROFILE: no`.
4. Plan the child as in Step 4, with these changes to the input:
   - `TASK: <the child's task>`
   - `SPLIT: no`
   - `PARENT: <absolute path of the tasks.json>`
   - `OUT_PATH: <workspace root>/<the child's plan>`
5. Print `### <id>: <title>`, then follow Step 5 for this child. A child never splits.

## Step 7 - Print the result

Print each reply **verbatim**. The one exception: with `--no-questions`, do not print a plan reply whose `QUESTIONS:` block has questions, because its revise reply replaces it. A plan reply is `Saved plan to <path>` plus the plan's `## Blockers` and `## Confidence` lines and its `QUESTIONS:` block. Do not summarize or repeat the rest of the plan; the file is the source of truth.

This prompt's only job is: parse -> resolve paths -> consent -> plan -> clarify, split or questions -> print. Do not edit any file but the ones your planner instructions allow, and do not improvise.
