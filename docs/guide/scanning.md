# Scanning your repo

`whygraph scan` builds the evidence database. It's the command you run after `init`, and again
whenever you want WhyGraph current. It's idempotent - each run picks up new commits and backfills
what's missing.

```bash
whygraph scan
```

## What a scan does

A scan runs up to four ordered phases. Each prints a header numbered against the phases that will
actually run this time, so a `--no-remote --skip-analyze` pass shows `Phase 1/2` and `Phase 2/2`
rather than gaps.

1. **Structural crawl** - the git crawler and, when a remote provider is enabled, the GitHub crawler,
   running concurrently. Records commits and the files each one touched, links PRs and issues to
   commits, and flags [branch membership](#how-whygraph-sees-branches). The history walk deliberately
   is **not** first-parent, so work merged from feature branches is not skipped.
2. **PR-origin recovery** *(optional)* - squash-merge recovery. When a PR was squash-merged, one
   targeted `git fetch` of the PR's original head brings its feature-branch commits into the evidence
   without polluting area history. Needs the network, so it is skipped under `--no-remote`.
3. **Author identity** - resolves commit addresses into one row per human. Local-only, no network and
   no token, so it runs on every scan including the offline git-hook path.
4. **LLM descriptions** *(optional)* - writes a short description of each commit's diff with the
   configured provider. The slow, token-heavy long pole, so it runs strictly last and alone.

**CodeGraph is not a phase.** The index refresh - `codegraph init -i` on the first run, `codegraph
sync -q` after - is a background task started before phase 1 and joined after the last one, so it
overlaps the whole crawl. A failure warns rather than aborting, since only the rationale and evidence
*tools* need CodeGraph.

Blame is *not* recorded at scan time. It's computed on demand when an evidence lookup needs it, which
is why a scan doesn't have to re-blame the repo every run.

### The results panel

A scan closes with a summary panel: one row per phase with a status glyph, a one-line summary, and its
timing, plus the paths to the database and the scan log. A phase that warned - a failed CodeGraph
refresh, a [bulk branch demotion](#how-whygraph-sees-branches) - shows `⚠` on its row, so a problem
isn't something you have to catch scrolling past.

Full detail for every run goes to **`.whygraph/scan.log`**, not the terminal. That's where to look
when a phase reports something you want to dig into.

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

Because the hooks run detached, their output isn't on your terminal. It goes to
**`.whygraph/logs/hooks.log`** - the place to look when a background rescan seems not to be
happening.

!!! warning "Hooks need `whygraph` on the PATH of whatever runs git"
    Each hook exits quietly when it can't find `whygraph`, so nothing breaks - but nothing rescans
    either. That bites GUI clients (Sourcetree, Tower, JetBrains, VS Code), which often launch with
    a minimal environment that excludes `~/.local/bin`. If you commit from one, symlink the shim
    into a system path: `sudo ln -sf ~/.local/bin/whygraph /usr/local/bin/whygraph`.

### Choosing which hooks to install

`[scan].hooks` in `whygraph.toml` governs the set, and **`whygraph init` makes `.git/hooks` match
it exactly**. Interactive `init` asks whether to install them; edit the value directly and re-run
`whygraph init` to apply a change without being asked.

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
clones the repo. A re-run seeds its prompt from the existing value, so declining hooks once isn't
quietly undone the next time you run `init`.

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
velocity numbers, area history, and the [chat assistant's](chat.md#statistics-are-aggregate-only)
statistics. It is still recorded, still searchable, and still evidence - it just isn't counted as
shipped.

Alongside the flag, each commit records **`first_seen_ref`**: the branch it was first seen on, or
`refs/pull/<N>/head` when it arrived through squash-merge recovery. `NULL` means it was already on
the default branch. It's written once and never rewritten, so a later demotion doesn't erase where a
commit came from. Rename tracking uses it to scope path history to the default branch *or* your
current branch - which keeps an in-flight rename visible without letting an abandoned branch pollute
history forever.

Membership is recomputed on **every** scan, so the database self-heals:

- Merge a branch and the next scan promotes its commits to the default branch.
- Squash-merge it and the originals stay off-branch, correctly - a squash creates a *new* commit.
- Force-push a commit away and the next scan demotes it, with a warning naming the count. The row is
  kept: a commit that no longer exists on any branch is still valid evidence for why the code looks
  the way it does.

Shallow clones (`git clone --depth=1`) skip the recompute entirely - a truncated view of history
would otherwise demote nearly everything.
