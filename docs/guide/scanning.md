# Scanning your repo

A scan builds the evidence database and refreshes the CodeGraph index. It's idempotent - each run
picks up new commits and backfills what's missing.

**In the portal you don't run it by hand.** The [portal](../portal/index.md) runs every scan itself, as
a `whygraph scan` child process in the project's folder: the first scan during
[project setup](../portal/projects.md#first-scan), then **Scan now**, **Sync now** (GitHub clones), the
git hooks, and a periodic catch-up. Progress and the log of each run are on the project's **Scans**
page. How runs are triggered, queued and coalesced is in the
[CLI reference](../reference/cli.md#scans-in-the-portal).

The command is still there for headless use - a CI job, or a checkout that is not in the portal:

```bash
whygraph scan
```

!!! note "`whygraph scan` refuses in a portal-managed repository"
    Once the portal has initialized a repository it is scanned by the portal only. Running
    `whygraph scan` there exits with status `2` and points at the project in the portal. To use the
    repo headless again, remove it from the Projects page or delete `.whygraph/portal.json`.

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
overlaps the whole crawl. An index built by a CodeGraph with a different *extraction version* (after
a CodeGraph upgrade, or a `.codegraph/` left by WhyGraph 1.x) is rebuilt in full with `codegraph
index` instead, since `sync` would keep it as it is. A failure warns rather than aborting, since only the rationale and evidence
*tools* need CodeGraph.

Blame is *not* recorded at scan time. It's computed on demand when an evidence lookup needs it, which
is why a scan doesn't have to re-blame the repo every run.

### The results panel

A scan closes with a summary panel: one row per phase with a status glyph, a one-line summary, and its
timing, plus the paths to the database and the scan log. A phase that warned - a failed CodeGraph
refresh, a [bulk branch demotion](#how-whygraph-sees-branches) - shows `⚠` on its row, so a problem
isn't something you have to catch scrolling past.

Full detail for every run goes to **`.whygraph/scan.log`**, not the terminal. That's where to look
when a phase reports something you want to dig into. (In the portal, the same summary and the tail of
the log are on the run's page.)

## Flags

| Flag | Default | What it does |
|---|---|---|
| `--skip-analyze` | off | Skip the per-commit LLM phase. Git and GitHub crawlers still run; descriptions backfill lazily and on a later full scan. |
| `--codegraph / --no-codegraph` | on | Refresh the CodeGraph index concurrently with the crawl. |
| `--codegraph-image TEXT` | pinned tag | Override the Docker image for the CodeGraph fallback. Ignored when a local `codegraph` binary is found. |
| `--remote / --no-remote` | on | Crawl the remote for PRs and issues per `[scan].forge`. `--no-remote` is a fast, offline, token-free scan. |
| `--pr-origins / --no-pr-origins` | on | Recover a squash-merged PR's original commits. Needs the network, so it's skipped under `--no-remote`. |
| `--progress json` | off | Emit JSON lines on stdout instead of the terminal display, for tools that drive a scan. See the [event reference](../reference/cli.md#json-progress). |

A common fast pass while iterating:

```bash
whygraph scan --no-remote --skip-analyze
```

!!! tip "Lazy backfill"
    Skipping descriptions doesn't lose them. The MCP tools backfill a commit's description on demand
    when they need it, and a later full `whygraph scan` fills in the rest. Start fast, enrich later.

## Keep it fresh

You don't have to re-scan by hand. When the portal initializes a local project it installs git hooks
that ask it to refresh WhyGraph and CodeGraph in the background as you work - there's no daemon beyond
the portal itself and no command to run.

Four hooks are wired, covering every git event that can change the tree or add commits:

| Hook | Fires on |
|---|---|
| `post-commit` | `git commit`, `git commit --amend` |
| `post-merge` | `git pull`, `git merge` |
| `post-rewrite` | `git rebase`, including `git pull --rebase` |
| `post-checkout` | `git switch` / `git checkout` to another branch |

Each hook **never scans locally**. It POSTs a request to the running portal
(`/api/projects/<slug>/scans`, with `curl`) and returns at once, so commits stay instant. The portal
runs an incremental scan - git history and a CodeGraph `sync` only, no LLM and no remote calls - so it
is offline and token-free. Rapid commits coalesce into one follow-up scan, and the latest `HEAD` wins.

The hooks run a shared helper that lives in the git directory, at `.git/whygraph/whygraph-scan` (in a
linked worktree, the main repository's git directory), never in the working tree - a checkout or pull
cannot replace it. Repositories set up by an earlier build had it at `.whygraph/hooks/whygraph-scan`;
the next Initialize or hooks change moves it. The helper finds the portal through
`.whygraph/portal.env`, a two-line file (`slug`, `port`) it **parses and never sources**, and ignores
if git tracks it or it is a symbolic link. It never writes through a symbolic link either. An existing hook of your own is appended to behind a
sentinel guard, never overwritten. `post-checkout` skips the two cases that can't have changed
anything - a file checkout (`git checkout -- somefile`) and `git switch -c` at the current commit.

**When the portal is down**, the request fails quietly and one line goes to
**`.whygraph/logs/hooks.log`** - the place to look when a background rescan seems not to be happening.
Nothing is lost: when the portal next starts, it **catches up**, queuing a scan for every local project
whose checkout moved past its last scanned commit, and repeats that check every 15 minutes.

!!! warning "Hooks need `curl` on the PATH of whatever runs git"
    The helper exits quietly when it can't find `curl`, so nothing breaks - but nothing rescans
    until the portal's next catch-up. That bites GUI clients (Sourcetree, Tower, JetBrains, VS Code),
    which often launch with a minimal environment. They do not need `whygraph` on the PATH - only `curl`.

### Choosing which hooks to install

`[scan].hooks` governs the set. In the portal it is the four **Git hooks** checkboxes in a project's
configuration, and saving a change reconciles `.git/hooks` to match. GitHub clones get no hooks (their
**Sync** does the same job).

```toml
[scan]
hooks = true                              # all four (the default)
# hooks = false                           # none
# hooks = ["post-commit", "post-merge"]   # only these two
```

The reconcile works in **both directions**. Shrinking the list *removes* the hooks you dropped -
you don't have to undo them by hand - and growing it adds them back. Setting `false` strips all
four and deletes the shared helper, leaving any foreign hook content of your own intact. Removing the
project from the portal strips them too.

!!! note "Hooks stay fast on purpose"
    The hooks deliberately skip the remote and LLM phases so they never slow a commit. For PRs,
    issues, and fresh descriptions, use **Scan now** in the portal now and then.

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
