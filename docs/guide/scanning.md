# Scanning your repo

A scan builds the evidence database and refreshes the CodeGraph index. It's idempotent - each run
picks up new commits and backfills what's missing.

**In the portal you don't run it by hand.** The [portal](../portal/index.md) runs every scan itself, as
a scan child process in the project's folder: the first scan during
[project setup](../portal/projects.md#configure), then **Scan now**, the git hooks, and a periodic
catch-up (in [production mode](../deploy/production.md#keeping-projects-current), GitHub's push
webhooks and an hourly check instead). Progress and the log of each run are on the project's
**Scans** page. How runs are triggered, queued and coalesced is in the
[CLI reference](../reference/cli.md#scans-in-the-portal).

## Watching a scan and the history

A project's **Scans** page lists every run, newest first, and a run opens on its own page. A run is
titled by what it was and when - *Full rescan - 9 Oct 2026, 14:02*, *Quick rescan*, *First scan*,
*Import*, *Describe*, *Commit hook*, *GitHub push*, *Sync from GitHub* or *Scheduled check* - never
by a run number (the number is only in the address). Quick versus full is visible on every row.

**The run page** shows who asked for it and when it was queued, started and finished, then the
phases. Every phase is named from the start ("Phase 2 of 4"), so you can see what is still to come;
a phase that never ran because an earlier one failed reads *Skipped*, and a run you stopped shows a
grey stop mark on the phase it was in, not a red failure. Per-task progress bars (commits described
so far) stay on this page. The outcome says what happened - *Imported and scanned*, *Fetched new
commits and scanned*, *No new commits* (never shown for a first run) or *Finished in 3m 10s* - and a
failed run explains itself in plain words with **Retry**, **Open settings** (for a missing key or
token) and **Open the log**; the raw error is under **Show details**. A cancelled run says
*Cancelled by you*, *Cancelled by* a colleague, or *Stopped: monthly budget reached*.

**Cost.** A project admin (anyone who can see the project's usage) also gets the cost of each run: the
LLM calls and what they cost on the run page, with *estimated $X before the run* when a full run was
queued with an estimate, and a **Cost** column in the history showing the actual cost with the
estimate beneath it. On an organization portal the **View these calls** link opens the calls behind
that cost on the [Usage & cost](../portal/usage.md) page. Without access to usage the cost column and lines are
simply absent.

**The history** loads fifty runs at a time (**Load more** fetches the next page) and filters by
**Status**, **Trigger**, **Requested by** (*Me*, *System*, or - for organization admins - a member) and
**Type** (*Full*, *Quick*, or *Sync* for a GitHub fetch that found nothing to scan). The filters are in
the address, so a filtered view can be bookmarked or shared; **Clear filters** resets them.

!!! note "The run log in production"
    On a production portal the first line of a run's log is `$ scan <flags>` (for example
    `$ scan --skip-analyze --no-remote`, or just `$ scan` for a full scan) - the flags only, never the
    real command line - and paths inside the server's copy of the
    repository appear as `.` and `<data>`, so a log never reveals where the portal keeps its files.

## What a scan does

A scan runs up to four ordered phases. The run's page numbers them against the phases that will
actually run this time, so a Quick rescan shows "Phase 1 of 2" and "Phase 2 of 2" rather than gaps.

1. **Structural crawl** - the git crawler and, when a remote provider is enabled, the GitHub crawler,
   running concurrently. Records commits and the files each one touched, links PRs and issues to
   commits, and flags [branch membership](#how-whygraph-sees-branches). The history walk deliberately
   is **not** first-parent, so work merged from feature branches is not skipped.
   After a repository's first crawl, the GitHub crawler asks only for the pull requests and issues
   created or updated since the previous crawl (with a 10-minute overlap) and updates the changed
   ones in place.
2. **PR-origin recovery** *(optional)* - squash-merge recovery. When a PR was squash-merged, one
   targeted `git fetch` of the PR's original head brings its feature-branch commits into the evidence
   without polluting area history. Needs the network, so it is skipped in a Quick rescan.
   A pull request whose branch ref is gone from GitHub is set aside and not retried until it changes
   on GitHub; the others are still recovered.
3. **Author identity** - resolves commit addresses into one row per human. Local-only, no network and
   no token, so it runs on every scan including the offline git-hook path.
4. **LLM descriptions** *(optional)* - writes a short description of each commit's diff with the
   configured provider. The slow, token-heavy long pole, so it runs strictly last and alone.

**CodeGraph is not a phase.** The index refresh - `codegraph init -i` on the first run, `codegraph
sync -q` after - is a background task started before phase 1 and joined after the last one, so it
overlaps the whole crawl. An index built by a CodeGraph with a different *extraction version* (after
a CodeGraph upgrade, or a `.codegraph/` left by WhyGraph 1.x) is rebuilt in full with `codegraph
index` instead, since `sync` would keep it as it is. A failure warns rather than aborting, since only the rationale and evidence
*tools* need CodeGraph. The warning also shows on the project's Overview, and until a refresh
succeeds the `codegraph_*` tools answer that the index could not be refreshed. Every CodeGraph process
ignores global and system git configuration, so a global `core.excludesFile` does not hide files from
the index.

Blame is *not* recorded at scan time. It's computed on demand when an evidence lookup needs it, which
is why a scan doesn't have to re-blame the repo every run.

### The results summary

A scan closes with a summary: one row per phase with a status, a one-line summary, and its timing. A
phase that warned - a failed CodeGraph refresh, a [bulk branch demotion](#how-whygraph-sees-branches) -
is marked on its row, so a problem isn't something you have to catch scrolling past.

A run's full log is on its page under **Scans**: that's where to look when a phase reports something
you want to dig into.

## Quick and full rescans

In the portal, **Scan now** offers two kinds of run:

| Run | What it does |
|---|---|
| **Quick rescan** | Git history and the CodeGraph index only: no LLM and no remote calls (no GitHub PRs or issues), so it is fast, offline and token-free. This is also what the git hooks ask for. |
| **Full rescan** | Everything a Quick rescan does, plus the GitHub crawl (after the first one, only what changed since), squash-merge recovery and the LLM description of each commit. |

!!! tip "Lazy backfill"
    Skipping descriptions doesn't lose them. The MCP tools backfill a commit's description on demand
    when they need it, and a later Full rescan fills in the rest. Start fast, enrich later.

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
the next set-up or hooks change moves it. The helper finds the portal through
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
configuration, and saving a change reconciles `.git/hooks` to match. A production portal's copies of
GitHub repositories get no hooks (GitHub's push webhooks do the same job).

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
