# Scanning your repo

`whygraph scan` builds the evidence database. It's the command you run after `init`, and again
whenever you want WhyGraph current. It's idempotent — each run picks up new commits and backfills
what's missing.

```bash
whygraph scan
```

## What a scan does

A scan runs several phases:

1. **Git crawl** — walks first-parent history and records commits, authors, and blame.
2. **Remote crawl** *(optional)* — pulls PRs and issues per `[scan].provider`, and links them to
   commits. Off unless you enable a provider.
3. **CodeGraph index refresh** — `codegraph init -i` on the first run, `codegraph sync` after. Runs
   concurrently with the crawl. A failure here warns rather than aborting, since only the rationale
   and evidence *tools* need CodeGraph.
4. **LLM descriptions** — writes a short description of each commit's diff with the configured
   provider.

It also handles **squash-merge recovery**: when a PR was squash-merged, `--pr-origins` does one
targeted `git fetch` of the PR's original head, so its feature-branch commits enrich the evidence
without polluting area history.

## Flags

| Flag | Default | What it does |
|---|---|---|
| `--no-llm-descriptions` | off | Skip the per-commit LLM phase. Git and GitHub crawlers still run; descriptions backfill lazily and on a later full scan. |
| `--codegraph / --no-codegraph` | on | Refresh the CodeGraph index concurrently with the crawl. |
| `--codegraph-image TEXT` | pinned tag | Override the Docker image for the CodeGraph fallback. Ignored when a local `codegraph` binary is found. |
| `--remote / --no-remote` | on | Crawl the remote for PRs and issues per `[scan].provider`. `--no-remote` is a fast, offline, token-free scan. |
| `--pr-origins / --no-pr-origins` | on | Recover a squash-merged PR's original commits. Needs the network, so it's skipped under `--no-remote`. |

A common fast pass while iterating:

```bash
whygraph scan --no-remote --no-llm-descriptions
```

!!! tip "Lazy backfill"
    Skipping descriptions doesn't lose them. The MCP tools backfill a commit's description on demand
    when they need it, and a later full `whygraph scan` fills in the rest. Start fast, enrich later.

## Keep it fresh

Don't want to re-scan by hand? Install git hooks once, and new commits refresh WhyGraph and CodeGraph
on the fly:

```bash
whygraph hooks install
```

This wires `post-commit`, `post-merge`, and `post-rewrite` to run
`whygraph scan --no-remote --no-llm-descriptions` **in the background**. Git history and a CodeGraph
`sync` only — no LLM, no remote calls — so commits stay instant and the scan is offline and
token-free.

The hooks are detached and single-flight: rapid commits coalesce instead of stacking, and the latest
`HEAD` always wins. An existing hook of your own is appended to behind a sentinel guard, never
overwritten.

Check or remove them any time:

```bash
whygraph hooks status
whygraph hooks uninstall
```

!!! note "Hooks stay fast on purpose"
    The hooks deliberately skip the remote and LLM phases so they never slow a commit. For PRs,
    issues, and fresh descriptions, run a full `whygraph scan` now and then.
