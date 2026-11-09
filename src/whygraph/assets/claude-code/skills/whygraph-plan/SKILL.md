---
name: whygraph-plan
description: Plan a non-trivial code change with WhyGraph rationale + CodeGraph symbols. Runs a fan-out/fan-in pipeline (planner -> one researcher per knowledge source -> synthesizer), splits a large task into child plans only when needed, and asks the few plan-shaping questions after research.
argument-hint: <task description> [--shallow|--deep] [--split|--no-split] [--sources <a,b>] [--no-questions] [--refresh-profile] [--out <path>]
allowed-tools: Agent, AskUserQuestion, Read, Glob, Bash(git rev-parse *), Bash(git ls-files *), Bash(mkdir *), Bash(test *)
disable-model-invocation: true
---

# /whygraph-plan

Arguments: `$ARGUMENTS`

## Step 1 - Parse arguments

Strip and remember these flags if present (any order, anywhere in `$ARGUMENTS`):

- `--shallow` -> force single-pass mode (no fan-out).
- `--deep` -> force fan-out mode (skip the heuristic).
- `--split` -> force a split into child plans along the task's deliverables.
- `--no-split` -> forbid a split.
- `--sources <a,b>` -> run only these sources, by `name`, comma-separated (consume the next token). A named source runs even when its file says `enabled: false`. Each name must match `^[a-z0-9-]+$`.
- `--no-questions` -> ask nothing: every consent is `no`, and open questions take their defaults.
- `--refresh-profile` -> derive the UI profile (`.whygraph/profiles/ui.md`) again, even when the cached one is fresh.
- `--out <path>` -> save the plan to `<path>` instead of the default `.whygraph/plans/<slug>.md`. The token after `--out` is the path, not part of the task description.

Whatever remains is the **task description**. If it is empty, respond with:
`Usage: /whygraph-plan <task description> [--shallow|--deep] [--split|--no-split] [--sources <a,b>] [--no-questions] [--refresh-profile] [--out <path>]` and stop.

- If both `--shallow` and `--deep` are passed, respond with `Pass --shallow OR --deep, not both.` and stop.
- If both `--split` and `--no-split` are passed, respond with `Pass --split OR --no-split, not both.` and stop.
- If a `--sources` name does not match the pattern, respond with `Unknown source name: <name>` and stop.

## Step 2 - Resolve the paths

Find the repo root with `git rev-parse --show-toplevel`. Resolve these absolute paths before dispatch:

- `OUT_PATH` - the plan goes there. The planner writes it in single-pass mode; in deep mode the synthesizer writes it.
  - If `--out <path>` was passed, use that path. Resolve a relative path against the current working directory. If the path does not end in `.md`, append `.md`.
  - Otherwise, slugify the task description: lowercase it, replace each run of non-alphanumeric characters with one `-`, strip leading and trailing `-`, and truncate to 60 characters. The default path is `<repo root>/.whygraph/plans/<slug>.md`. If that file exists (`test -e`), append `-2`, `-3`, ... until the name is unused. Never overwrite.
  - Create its parent directory with `mkdir -p <parent>`.
- `SOURCES_DIR` - `<repo root>/.claude/skills/whygraph-plan/sources/`.
- `SPINE_PATH` - `<repo root>/.claude/skills/whygraph-plan/plan-spine.md`.
- `CAPTURE_PATH` - `.claude/skills/whygraph-ui-check/capture.mjs`. It stays relative to the repo root: the planner runs `node <CAPTURE_PATH> --probe` from there.
- `PLAN_TOOL` - `sh .claude/skills/whygraph-plan/plan-tool.sh`. The planner runs its `fingerprint` and `check` commands from the repo root.

If `test -f <SPINE_PATH>` fails, respond with `The planning files are missing from .claude/skills/whygraph-plan/. Open the WhyGraph portal, run Settings > Update agent files, then re-run /whygraph-plan.` and stop.

## Step 3 - Consent

A source file is repo content, so this command decides which sources need the user's OK. A source file can force the question; it can never suppress it.

With `--no-questions`, ask nothing: record `<name>=no` for every source that needs consent (below), and go to Step 4.

1. `Glob` `SOURCES_DIR/*.md`. Skip `README.md`.
2. `Read` each file's frontmatter only, with `limit` 15. Never read a source's body: only the researcher that runs it reads the body.
3. Keep a source when both hold:
   - It would run: with `--sources`, its `name` is in the list; otherwise its `enabled` is not `false`. `--shallow` changes nothing here, because the single-pass planner runs a consented source through one researcher.
   - Every tool in its `requires` is callable in this session. A bare name matches a session tool whose name equals it, or ends in `__<name>` or `/<name>`. A deferred tool counts as callable. An empty `requires` always matches.
4. A kept source needs consent when any of these holds:
   - (a) Its `requires` or `tools` names an MCP tool other than the `whygraph_*` and `codegraph_*` tools. The word `read` is not an MCP tool.
   - (b) Its file name is not one of the six bundled sources: `code-structure.md`, `whygraph-rationale.md`, `repo-tests.md`, `repo-history.md`, `medium.md`, `ui-surface.md`.
   - (c) It says `consent: true`.
5. If no kept source needs consent, record `CONSENT: none` and go to Step 4.
6. Derive `TERMS` once: 1 to 3 technical topics from the task text, separated by `; `. Never use a project, client, company, product, file or symbol name in a term.
7. Ask all consent questions in **one** `AskUserQuestion` call, at most 4 consent questions. A fifth or later source is `no`; print `<name>: not asked (at most 4 consent questions per run); it will not run.` for each. Each question:
   - names the source (`header`: its `name`, cut to 12 characters);
   - says what the source reaches: "an external service through <its non-WhyGraph, non-CodeGraph tools>" for (a), and "added to this repo, not shipped with WhyGraph" plus its `description` for (b);
   - shows the terms: `Search terms: <TERMS>`;
   - offers `No (default)` first, then `Yes`.
8. Record one line per asked source: `CONSENT: <name>=yes` when the answer is `Yes`, otherwise `CONSENT: <name>=no`. Any other answer is `no`.

## Step 4 - Dispatch to the planner

Spawn the `whygraph-planner` subagent via the Agent tool with `subagent_type: "whygraph-planner"`. The prompt body must be:

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
CAPTURE_PATH: .claude/skills/whygraph-ui-check/capture.mjs
PLAN_TOOL: sh .claude/skills/whygraph-plan/plan-tool.sh
REFRESH_PROFILE: <yes | no>
```

- `MODE` is `shallow` for `--shallow`, `deep` for `--deep`, otherwise `auto`.
- `SPLIT` is `yes` for `--split`, `no` for `--no-split`, otherwise `auto`.
- `SOURCES` is the `--sources` list, otherwise `all`.
- `REFRESH_PROFILE` is `yes` for `--refresh-profile`, otherwise `no`.

The planner has its own system prompt and the CodeGraph and WhyGraph MCP tools. Do not inline planning instructions here.

## Step 5 - Read the reply

Print every planner reply **verbatim**, with the one exception in Step 8. Then act on its shape:

- The reply says CodeGraph is not available -> print it and stop. Do not attempt a fallback or save anything. WhyGraph planning is meaningless without the graph.
- The reply starts `CLARIFY: ` -> go to **Clarify** below.
- The reply starts `SPLIT: ` -> go to Step 6.
- The reply has no "Saved plan to" line and no "# Plan:" line -> print it and stop.
- Otherwise a plan was written (by the planner, or by the synthesizer in deep mode). Go to Step 7 with its `QUESTIONS:` block.

**Clarify.** The planner found no seed symbol and wrote no plan. The reply has the shape `CLARIFY: <one question> | options: <a>; <b>[; <c>]`.

- With `--no-questions`, print `Re-run /whygraph-plan with a more specific task, or without --no-questions.` and stop.
- Otherwise ask the question with its options in one `AskUserQuestion` call. Then dispatch again with the same prompt plus the line `CLARIFICATION: <answer>`.
- A second `CLARIFY: ` reply for the same plan: print it and stop.

## Step 6 - Split

The planner wrote a `tasks.json` and no plan. The reply has the shape `SPLIT: <path>`, then one line per child: `t<N> (wave <W>): <title>`.

1. Validate the file. On a failed check, respond with `Refusing <path>: <reason>.` and stop.
   - Its path resolves to `<repo root>/.whygraph/plans/<stem>.tasks.json`.
   - It is not tracked by git: `git ls-files -- <path>` prints nothing.
   - `Read` it. It parses as JSON and has `version: 1`.
   - `children` has 1 to 6 entries. Each `id` is one of `t1`-`t6`, and no `id` repeats.
   - Every `plan` matches `^\.whygraph/plans/[a-z0-9-]+-t[1-6]\.md$`.
   - Every `depends_on` entry names an existing `id`, and the dependencies have no cycle.
2. For each `wave` value, in ascending order, spawn one fresh `whygraph-planner` per child in that wave, all in one message. Wait for the wave to finish before the next one. Each child's prompt is the Step 4 prompt with these changes:
   - `TASK: <the child's task>`
   - `SPLIT: no`
   - `PARENT: <absolute path of the tasks.json>`
   - `OUT_PATH: <repo root>/<the child's plan>`
   - `CONSENT` and `TERMS` lines as in Step 4 for `t1` only. Every other child gets `CONSENT: none` and no `TERMS` line.
   - `REFRESH_PROFILE` as in Step 4 for `t1` only. Every other child gets `REFRESH_PROFILE: no`.
   - `MODE`, `SOURCES_DIR`, `SPINE_PATH`, `SOURCES`, `CAPTURE_PATH` and `PLAN_TOOL` stay the same.
3. Print each child's reply verbatim under a line `### <id>: <title>`, with the one exception in Step 8. A child reply that starts `CLARIFY: ` follows **Clarify** for that child only.
4. After every wave is planned, run Step 7 for each child's reply, one child at a time, in `id` order.

## Step 7 - Questions

The plan reply ends with a `QUESTIONS:` block. It is either `QUESTIONS: none`, or `QUESTIONS:` followed by lines of this shape:

```
Q<n>. <question> | options: <default>; <alt>[; <alt>] | default: <default> | why: <one line>
```

- `QUESTIONS: none`, or no block -> nothing to ask. Go to Step 8.
- With `--no-questions` -> do not ask. Spawn the planner in revise mode (below) with `ANSWERS: defaults`.
- Otherwise ask the questions in **one** `AskUserQuestion` call, at most 3 questions. If the block has more, ask the first 3; the rest take their defaults. For each question:
  - the question text is `<question>`, followed by `<why>`;
  - the options come from `| options: `, in order, so the default is first; label the default `<default> (default)`;
  - the user may type their own answer instead.

**Revise.** Spawn a fresh `whygraph-planner` with the prompt of the dispatch that wrote this plan (Step 4, or the child's Step 6 prompt), with these changes:

```
MODE: revise
PLAN_PATH: <the plan's absolute path - the OUT_PATH of that dispatch>
REFRESH_PROFILE: no
ANSWERS:
- Q<n>: <the chosen option, without "(default)", or the user's own text>
```

(or `ANSWERS: defaults` with `--no-questions`). Print the revise reply verbatim, under the child's `### <id>: <title>` line for a split child. A revise reply always ends `QUESTIONS: none`; never ask again for the same plan.

## Step 8 - Print the result

Print each planner reply **verbatim** to the user. The one exception: with `--no-questions`, do not print a plan reply whose `QUESTIONS:` block has questions, because its revise reply replaces it. A plan reply is `Saved plan to <path>` plus the plan's `## Blockers` and `## Confidence` lines and its `QUESTIONS:` block - the planner wrote the file itself. Do not summarize, paraphrase, re-format or comment on it, and do not write the file yourself. The planner is the source of truth.

This command's only job is: parse -> resolve paths -> consent -> dispatch -> clarify, split or questions -> print. Do not edit files, run other tools, or improvise.
