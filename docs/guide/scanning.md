# Scanning your repo

`whygraph scan` builds the evidence database. It's the command you run after `init`, and again
whenever you want WhyGraph current. It's idempotent - each run picks up new commits and backfills
what's missing.

```bash
whygraph scan
```

## What a scan does

A scan runs several phases:

1. **Git crawl** - walks first-parent history and records commits, authors, and blame.
2. **Remote crawl** *(optional)* - pulls PRs and issues per `[scan].provider`, and links them to
   commits. Off unless you enable a provider.
3. **CodeGraph index refresh** - `codegraph init -i` on the first run, `codegraph sync` after. Runs
   concurrently with the crawl. A failure here warns rather than aborting, since only the rationale
   and evidence *tools* need CodeGraph.
4. **LLM descriptions** - writes a short description of each commit's diff with the configured
   provider.

It also handles **squash-merge recovery**: when a PR was squash-merged, `--pr-origins` does one
targeted `git fetch` of the PR's original head, so its feature-branch commits enrich the evidence
without polluting area history.

## Flags

| Flag | Default | What it does |
|---|---|---|
| `--skip-analyze` | off | Skip the per-commit LLM phase. Git and GitHub crawlers still run; descriptions backfill lazily and on a later full scan. |
| `--codegraph / --no-codegraph` | on | Refresh the CodeGraph index concurrently with the crawl. |
| `--codegraph-image TEXT` | pinned tag | Override the Docker image for the CodeGraph fallback. Ignored when a local `codegraph` binary is found. |
| `--remote / --no-remote` | on | Crawl the remote for PRs and issues per `[scan].provider`. `--no-remote` is a fast, offline, token-free scan. |
| `--pr-origins / --no-pr-origins` | on | Recover a squash-merged PR's original commits. Needs the network, so it's skipped under `--no-remote`. |

A common fast pass while iterating:

```bash
whygraph scan --no-remote --skip-analyze
```

!!! tip "Lazy backfill"
    Skipping descriptions doesn't lose them. The MCP tools backfill a commit's description on demand
    when they need it, and a later full `whygraph scan` fills in the rest. Start fast, enrich later.

## Keep it fresh

You don't have to re-scan by hand. `whygraph init` installs git hooks that refresh WhyGraph and
CodeGraph in the background as you work - there's no daemon and no separate command to run.

Four hooks are wired, covering every git event that can change the tree or add commits:

| Hook | Fires on |
|---|---|
| `post-commit` | `git commit`, `git commit --amend` |
| `post-merge` | `git pull`, `git merge` |
| `post-rewrite` | `git rebase`, including `git pull --rebase` |
| `post-checkout` | `git switch` / `git checkout` to another branch |

Each runs `whygraph scan --no-remote --skip-analyze` **in the background**. Git history and a
CodeGraph `sync` only - no LLM, no remote calls - so commits stay instant and the scan is offline
and token-free.

The hooks are detached and single-flight: rapid commits coalesce instead of stacking, and the latest
`HEAD` always wins. An existing hook of your own is appended to behind a sentinel guard, never
overwritten. `post-checkout` skips the two cases that can't have changed anything - a file checkout
(`git checkout -- somefile`) and `git switch -c` at the current commit.

### Choosing which hooks to install

`[scan].hooks` in `whygraph.toml` governs the set, and **`whygraph init` makes `.git/hooks` match
it exactly**. Edit the value, then re-run `whygraph init` - nothing changes until you do.

```toml
[scan]
hooks = true                              # all four (the default)
# hooks = false                           # none
# hooks = ["post-commit", "post-merge"]   # only these two
```

The reconcile works in **both directions**. Shrinking the list *removes* the hooks you dropped -
you don't have to undo them by hand - and growing it adds them back. Setting `false` strips all
four and deletes the shared helper, leaving any foreign hook content of your own intact.

Because the setting lives in the committed config, it survives re-runs and applies to everyone who
clones the repo.

!!! note "Hooks stay fast on purpose"
    The hooks deliberately skip the remote and LLM phases so they never slow a commit. For PRs,
    issues, and fresh descriptions, run a full `whygraph scan` now and then.

## How WhyGraph sees branches

WhyGraph records every commit it walks, but it distinguishes **shipped history** from work in
progress. Each commit row carries `on_default_branch`: `1` when the commit is reachable from the
default branch, `0` when it isn't.

The default branch is resolved from `origin/HEAD`, falling back to `origin/main` then
`origin/master`, and is judged as the union of that remote-tracking ref *and* your local branch of
the same name - so commits you've made on `main` but haven't pushed still count as shipped. For a
repo on `develop` or `trunk` where `origin/HEAD` isn't set, name it explicitly:

```toml
[scan]
default_branch = "develop"
```

The pre-scan panel shows what it resolved. If it says `unresolved`, branch flagging is off and every
commit is treated as on the default branch - the same behaviour as before this existed.

**What this means in practice:** unmerged work on a feature branch is excluded by design from
velocity numbers, area history, and the chat statistics surface. It is still recorded, still
searchable, and still evidence - it just isn't counted as shipped.

Membership is recomputed on **every** scan, so the database self-heals:

- Merge a branch and the next scan promotes its commits to the default branch.
- Squash-merge it and the originals stay off-branch, correctly - a squash creates a *new* commit.
- Force-push a commit away and the next scan demotes it, with a warning naming the count. The row is
  kept: a commit that no longer exists on any branch is still valid evidence for why the code looks
  the way it does.

Shallow clones (`git clone --depth=1`) skip the recompute entirely - a truncated view of history
would otherwise demote nearly everything.
