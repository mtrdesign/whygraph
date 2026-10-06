# Projects from a platform

A **platform** is a [production](../deploy/production.md) WhyGraph, where a team keeps its projects
and their history in one place. A **platform project** is a checkout on your machine **linked** to
one of those projects: your coding agent keeps talking to your local portal's MCP endpoint at
`/mcp/<slug>`, and the local portal answers from your working tree plus the platform's history. The
history is not copied to your laptop, and nothing is scanned with an LLM on it.

Use it when the team already imports the repository on a platform and you want your agent to see
that history, along with your own uncommitted work, without running a full local scan.

!!! note "Local mode only"
    Linking is done from a **local** portal. A production portal serves platform projects to
    linked portals over `/api/v1`; it never links to another one.

## Link a checkout

There are two ways in, and both end at the same wizard:

- From the platform: open the project's home page and use **Use with your agent**. Enter your local
  portal's port (`8765` unless you changed it) and press **Open in my local WhyGraph**. The link
  opens your local portal's `/link` page with the platform, organization and project filled in.
  The platform's page never calls your portal, so it cannot tell whether yours is running.
- From the local portal: **New project**, then **From a platform**. Enter the platform's address
  and a **machine name**, and press **Connect**.

The machine name is what the platform lists for this connection; it defaults to your host's name
(`whygraph up` passes it to the container) and you can edit it, with letters, digits, `.`, `_`,
`-` and spaces, up to 64 characters.

**Connect** sends your browser to the platform's consent page. Sign in there if you are not
already, check the project and the machine name, and press **Allow**. The browser returns to your
portal, which exchanges the one-time code for a **connection token** for that one project, held
encrypted in the portal's own database (see [Security](security.md#connected-portals)). Then:

1. **Pick the checkout.** The wizard lists repositories under your [shared
   folders](shared-folders.md) that match the project. A checkout matches when its `origin` names
   the platform project's repository (host, owner and name, so a mirror or an SSH host alias
   works), **or** the commit the platform last scanned exists in it. A project the platform has
   not scanned yet needs the `origin` match. Anything else is refused with `origin_mismatch`.
2. **Initialize.** The same preview as for a local project: `.gitignore` entries, the git hooks,
   the MCP entry and bundled files for each agent you select, and the two marker files. No
   database is created. See [Connecting agents](agents.md): a linked project uses the same agent
   files, with the same slug as the platform's project.
3. **First scan.** A CodeGraph index of your checkout. It reads no history and calls no LLM.

The slug is the platform's slug. If a project with that slug already exists on your portal, linking
is refused with `slug_taken`. Only members of the organization can link: an instance admin's
read-only access to an organization cannot.

## What runs where

| Question | Answered by |
|---|---|
| Who last changed these lines, and in which commit | Your machine, with `git blame` on your working tree |
| What a pushed commit was for, its pull requests and issues | The platform, from its own database |
| The rationale card of a symbol | The platform, from its shared cache |
| Every commit that touched a path | The platform |
| Symbols and the CodeGraph index | Your machine, your own index |
| Explorer, Chat, models, keys, descriptions | The platform. The local routes refuse and point there |

A linked project **never opens a local WhyGraph database**: there is no `.whygraph/whygraph.db` in
the checkout, and the code that would open one refuses. Its scans are **CodeGraph only**: no git
crawl, no LLM and no database. A scan request that asks for descriptions is refused.

The only setting a linked project keeps locally is `[scan].hooks`, which git hooks rescan the
checkout. Every other setting is the platform's, and the portal answers a change with `403
managed_on_platform` and the platform's address. The Explorer and Chat open on the platform.

## What leaves your machine

Only what the platform already holds, or could. Your portal blames the working tree locally, then
sends the platform:

- the **pushed** commits - those some `origin/*` ref in your checkout contains - with the line ranges
  they own, at the path they had in that commit;
- the target's **path**, only when a pushed revision tracks it (otherwise the path a pushed hunk
  came from);
- the symbol's **qualified name**, only when the line it is declared on blames to a pushed commit.

It never sends file contents, commit messages, uncommitted lines, or commits that exist only in your
checkout. Whether a commit is pushed is read from your `origin/*` refs as the last `git fetch` left
them; the portal never fetches for you.

## What your agent sees

The three tools and the resources work as on any project (see the [MCP
reference](../reference/mcp.md#linked-projects)), with two additions: every result carries a
`platform` block, and each commit WhyGraph could not take from the platform is labelled with a
`push_status`:

| `push_status` | Meaning | What to do |
|---|---|---|
| `uncommitted` | The lines are not committed yet | Commit |
| `not_pushed` | The commit is only in your checkout | Push it |
| `pending_scan` | Pushed to the default branch, but the platform has not scanned it yet | Wait; its webhook and hourly check usually take seconds |
| `not_on_default_branch` | Pushed to a branch other than the one the platform scans | Merge it |

Evidence the platform does supply carries no `push_status`. An item labelled this way has the commit
author, date and subject from git, no description, and no pull requests or issues.

Rationale cards are generated **on the platform**, by the organization's own models and keys, and
shared by everyone on the project: an agent that asks for a card nobody has generated yet spends the
organization's [hourly agent limit](../deploy/production.md#agent-limits), and an organization can set
it to zero so agents get only cards that exist.

### Offline

When the platform cannot be reached, nothing breaks. `whygraph_evidence_for` still returns your
blame, with every pushed commit labelled `pending_scan` or `not_on_default_branch` from your local
refs. The other tools and resources need the platform's history and report a clear error; a commit
resource falls back to its git message. The `platform` block says why.

## Statuses

Each linked project shows one status, on its card, its home page and in the `platform` block of every
tool result. It is updated by the calls your agent makes, and checked once when the portal starts and
when you open the project list. There is no background polling.

| Status | Meaning | What to do |
|---|---|---|
| `ok` | The platform answered | Nothing |
| `unreachable` | The platform did not answer | Your agent still gets your blame; try again later |
| `revoked` | The platform refused the token. The reason is shown: you or an admin revoked it, you were removed from the organization or left it, your account was disabled, or the token went unused (90 days, or one hour if never used) | **Reconnect** or remove |
| `revoked`, reason `project_access_removed` | You lost access to this project on the platform (it became Restricted, your role was removed or the organization default dropped) | Ask a project admin on the platform for access, or remove it; reconnecting cannot help |
| `removed` | The project, or its organization, was deleted | Remove it from this machine |
| `access_lost` | The platform can no longer read the repository, so its history may be out of date | An admin fixes it on the platform; nothing to do locally |
| `update_required` | The platform needs a newer WhyGraph than this portal | Update with `whygraph up` |

### Project roles

What your agent can do through a linked project follows your **project role** on the platform (see
[project roles](../deploy/production.md#project-roles)). A **viewer** can connect and read existing
evidence and rationale cards, but nothing spends the organization's LLM keys on their behalf, so no new
card is generated. Contributors and admins can generate cards.

### Reconnect

A revoked link is repaired in place. Link the **same checkout** again: the same path, platform,
organization, project and clone URL. The portal replaces the old token with the new one, resets the
status and keeps the project, its slug and its agent files. Any other collision keeps its refusal
(`slug_taken`, `duplicate`).

## Remove from this machine

**Settings**, then **Remove**, does what removing any project does (hooks, markers, optional agent
entries; your repository is never deleted) and first **revokes the connection token on the
platform**. It tells you when that failed, so you can revoke it yourself under **Connected portals**
on the platform. The platform's project is untouched, and removal is refused while a scan is running.
