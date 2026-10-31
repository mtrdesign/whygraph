---
name: whygraph-review
description: Check a diff against the recorded WhyGraph rationale - per hunk, whether a constraint or risk on the enclosing symbol's card is at stake. Reads cached cards only, no LLM spend, unless --generate is passed.
argument-hint: "[<base>] [--generate]"
allowed-tools: Agent, Read
disable-model-invocation: true
---

# /whygraph-review

Arguments: `$ARGUMENTS`

## Step 1 - Parse arguments

Strip and remember this flag if present (anywhere in `$ARGUMENTS`):

- `--generate` -> allow WhyGraph to generate a missing rationale card (one LLM call per card, charged to the project's budget). Default `false`: cached cards only, no LLM spend.

What remains is the optional **base**: at most one token, a branch, tag or commit to review against. It must match `^[A-Za-z0-9][A-Za-z0-9._/^~-]*$` (so it never starts with `-`). If more than one token remains, or the token does not match, respond with:
`Usage: /whygraph-review [<base>] [--generate]` and stop.

With no base, use `auto`: the working tree against `HEAD` when there are uncommitted changes, otherwise the branch against its merge base with the default branch.

## Step 2 - Dispatch to the reviewer

Spawn the `whygraph-reviewer` subagent via the Agent tool with `subagent_type: "whygraph-reviewer"`. The prompt body must be exactly:

```
BASE: <base | auto>
GENERATE: <true | false>
```

The reviewer has its own system prompt covering the diff, the hunk-to-symbol mapping, the card lookups and the output contract - do not inline review instructions here. Cards now carry `notes`; a hunk that contradicts a confirmed note is a finding.

## Step 3 - Print the result

When the reviewer returns, print its output **verbatim** to the user. Do not summarise, paraphrase, re-format, or comment. The reviewer is the source of truth.

This command's only job is: parse -> dispatch -> print. Do not edit files, run other tools, or improvise.
