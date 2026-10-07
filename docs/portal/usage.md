# Usage & cost

Every LLM call the portal makes is written to a **usage ledger**, so you can see what WhyGraph spends
on your provider keys - per project, model, task and (on a production portal) per person - and cap it
with **budgets**. Open it from **Usage & cost** in the sidebar.

!!! note "Estimates, not invoices"
    Keys are yours, and WhyGraph never reads your provider's billing. Every money figure is an
    **estimate** of the bill, labelled as such, unless the provider itself reported the cost (only
    OpenRouter does). Your provider's invoice is the source of truth.

## What is recorded

One row per **successful provider round trip**:

- each commit description, and each synthesis call for a large commit, during a scan or the lazy
  backfill;
- each rationale card generated (in the Explorer, by chat, or for an agent);
- each **round** of a chat turn, not one row per turn, since a turn with tool calls makes several.

A row holds the organization, project, task (`analyze`, `rationale` or `chat`), who triggered it, the
provider, the model asked for and the model that answered, which key paid (the project's, the
organization's, or the portal's environment), the tokens (input, output, cache reads and writes,
reasoning), the cost and where it came from, and a short **subject**: a commit hash, a file path or a
qualified name. **Never prompt text, never completion text, never chat content.**

What is **not** recorded:

- **A call that failed or was interrupted.** A provider error, a stream that dropped, or a closed
  browser tab writes nothing, so the ledger slightly under-counts. A call that finished but reported no
  usage is recorded with no tokens and no cost.
- **Headless `whygraph scan`** (CI, a plain checkout): it has no portal to write to.
- **A [linked project](platform-projects.md)'s calls** on your local portal. They run on the platform,
  which records them against the connection's owner. Your local portal records nothing for them.
- **Anything the provider bills you for outside WhyGraph**, and your own provider's discounts.

Figures are for a **calendar month in UTC**; the page says when the month resets ("resets 1 Nov, 00:00
UTC"). History is kept for [400 days](#retention-and-deletion).

## Provider-reported, estimated, unpriced

Each row's cost is worked out **once, when it is written**, and stored with the price version that
made it. A later price change never rewrites old months.

| Cost source | When | Counts toward money figures and budgets |
|---|---|---|
| **Provider-reported** | OpenRouter returned a `cost`. On a BYOK request the upstream provider's cost is added to it. | Yes |
| **Estimated** | The tokens reported times the price for that model, with cache reads and writes at their own rates | Yes |
| **Unpriced** | No price applies: the tokens are kept, the cost is not | No: tokens only |

A model with no price is **unpriced**. That includes every DeepSeek model by default (the bundled
table has no DeepSeek rows), Ollama, and any model that is not in the table. **A provider whose
`base_url` or `host` you changed is never priced from the bundled table**, because the other end may
be a different service with other rates; only your own [price override](#prices-and-overrides)
applies. The Usage & cost page and the Budgets tab say how many calls were unpriced, and unpriced
calls never count toward a budget. See
[What each provider reports](../reference/llm-providers.md#what-each-provider-reports).

## The page

The page opens on the current month. A range picker offers **This month**, **Last month**, the last 7,
30 or 90 days and the last 12 months, or any dates you choose (a range is at most 400 days). A tab for
each view:

| Tab | Shows |
|---|---|
| **Overview** | Spend this month, calls and tokens, a daily cost chart, the split into interactive and scans, and the top projects and models |
| **Projects** | Spend, calls and tokens for each project |
| **Members** | Each person's spend, tokens, calls, the split, and their top project. Production only |
| **Models** | The same, per provider and model |
| **Machines** | Calls made through [connected portals](../deploy/production.md#connected-portals), per machine. Production only |
| **Calls** | Every call, 50 to a page, newest first or costliest first, filterable by project, member, task, source, model, scan run or chat session |
| **Budgets** | Budgets, this month's crossings and the unpriced warning. See [Budgets](#budgets-and-the-hard-stop) |
| **Prices** | The price table and your overrides. See [Prices](#prices-and-overrides) |

Click a row in a breakdown to open the Calls tab filtered to it. A call row shows its time, project,
who, task, model, tokens and cost, and links to the scan run or chat session it belongs to.

!!! note "Triggered is not benefited"
    Every figure is split into **interactive** (chat, generating a rationale card, agents, the lazy
    backfill) and **scans started** (a full rescan or Describe now). A call is attributed to whoever
    **triggered** it, not whoever benefits from the result: one admin starting a Describe now pays for
    descriptions the whole team reads. The Members tab is accounting, not a ranking.

### Export

Every table has a **CSV** button. On Calls it exports one row per call; with a breakdown open, it
exports that breakdown. The file respects the range and filters, and is named like
`whygraph-usage-<org>-<group>.csv`. An export stops at **50,000 rows**: the response carries the
header `X-WhyGraph-Truncated: 1`, the file ends with the line `# truncated at 50000 rows`, and the page
tells you to narrow the range or add a filter. A cell that starts with `=`, `+`, `-` or `@` is
prefixed with `'` so a spreadsheet does not run it.

## Who triggered a call

| Trigger | Attributed to |
|---|---|
| Chat, Explorer **Generate**, the lazy backfill | The person using it |
| A full rescan or **Describe now** | The person who started it. When requests merge, the first person who asked for analysis |
| A connected portal's agent | The connection's owner, with the connection and machine name |
| A webhook, reconcile, hook or catch-up scan | **System** |

**System is nearly empty by design.** Webhook, reconcile, hook and catch-up scans are structure-only
(git history and the CodeGraph index, no LLM), so they cost nothing. System appears as a member row
(filter `member=system`) only if something unattended ever calls a model.

In local mode there is one user, and the calls are simply "you".

!!! info "Production: members and My usage"
    On a [production](../deploy/production.md) portal **owners and org admins see everyone**, and the
    organization's **Members** tab lists each person, with a drill-down page per person: cost over
    time, and by project, task and machine, with their most expensive calls. The **Members** page
    shows a "this month" figure per person, and a project's Access section shows what each person
    spent on it. Instance admins reading an organization see the same, read-only.

    **A member sees only their own usage**, on **My usage**: the **Usage & cost** item in the sidebar,
    and a summary of each organization on the **Account** page. The server forces the view to the
    caller; there is no filter to widen. Someone who later loses access to a project still sees their
    own past rows for it, by name, without a link. A removed member's rows stay, labelled with the
    name they had.

    **Machines** has one row per connected portal. Calls that did not come through one (chat, the
    Explorer, scans) are grouped as **Portal**. A connection that has since been deleted shows its
    machine name, or **Unknown machine** when even that is gone.

## Budgets and the hard stop

A **budget** is a monthly amount in US dollars, set by an owner or admin on the Budgets tab. It can
cover:

- the **organization** (in local mode, the whole portal);
- one **project** (also from its Settings, under **Usage**);
- one **member**, or every member by default, with personal overrides. Production only.

A project, member default or member budget cannot exceed the organization's (`422 budget_above_org`),
and the organization's cannot be set below an existing one underneath it (`409
budget_below_children`, which lists them). An amount is above 0 and at most 1,000,000. A call counts
against every budget that covers it; unpriced calls count against none.

An **admin cannot set or remove their own member budget or an owner's**, since that would cap the
person doing the capping. Owners can set anyone's.

A budget alone only **alerts**. Turn on its **hard stop** to enforce it.

### The hard stop

When a hard-stopped budget is used up, **LLM spend stops for everyone it covers** until the month
resets or someone raises it:

- an **organization** budget covers the whole organization, owners included;
- a **project** budget covers everyone on that project;
- a **member** budget covers that person in every project.

Those people behave like a Viewer for LLM spend: no chat, no new rationale card, no Describe now or
other full scan. **Everything else keeps working**: the Explorer, evidence and every already-written
description or card, MCP reads, quick scans (git and CodeGraph, no LLM), and **Cancel**, which is never
blocked.

Work already in flight stops at its next step:

- a **chat** turn stops before its next round, and its last message says "This chat has reached its
  monthly budget. You can still read everything that's already generated.";
- the lazy **backfill** stops before the next commit;
- a **running full scan** is cancelled as soon as a call crosses the cap. The run shows "Stopped:
  monthly budget reached", and the descriptions already committed are kept;
- a **queued** full scan that is blocked when it comes to run is downgraded to a structure-only scan
  rather than cancelled, so a merged webhook still gets its update. The run shows "LLM phase skipped:
  monthly budget reached".

A call already sent to the provider finishes, so spend can overshoot by the calls in flight (at most
`[analyze].max_workers` for a scan, one anywhere else).

Everything the stop refuses answers **`403 budget_exceeded`** with a `scope` of `org`, `project` or
`member`. The notice on the project's pages, in Chat and beside **Generate rationale** says whose
budget it is. If a person is both a Viewer and over a budget, the Viewer notice wins, since the role
is permanent.

### Alerts

Each budget is checked against **50, 75 and 100%** as spend is added:

- **Banners.** Owners and admins see one on Usage & cost and the Projects page, naming the organization
  and any project at or over 50%. A member sees their own budget at each threshold on every page. The
  50% and 75% banners can be dismissed for the month; the 100% one cannot.
- **Once a month.** Each threshold fires **once per budget per month**. Raising a budget does not
  re-arm a threshold already crossed, though the banners always show the live percentage. A budget
  deleted and set again is new, and can fire again. Lowering a budget below the spend counts as a
  crossing.
- **The Budgets tab** lists this month's crossings, because admins cannot read the audit log.
- **Audit events** on a production portal: `budget_threshold_crossed`, `budget_hard_stop_engaged` (at
  100% with the hard stop), `budget_set`, `budget_removed`, `price_override_set` and
  `price_override_removed`. See [the security event log](../deploy/production.md#the-security-event-log).
- If the month had unpriced calls, the tab warns that they are not counted toward budgets.

Nothing is sent outside the portal: no email, no Slack, no webhook.

## Prices and overrides

WhyGraph ships a price table with each release (the Prices tab shows its date) for the Anthropic and
OpenAI models it knows, per million tokens: input, output, cache reads and cache writes. An owner or
admin can **override** the price of any provider and model for their organization (to match a
negotiated rate, to price a DeepSeek or custom-endpoint model, or to correct a model the table
lacks), and **revert** it to the bundled value. Overrides apply to calls made **from then on**: past
rows keep the price they were written with. The first-scan estimate on a project uses the same table
and your overrides, and says "your organization's prices" when an override applied.

## Retention and deletion

Calls are kept for **400 days** and then deleted, like the audit log. Budget crossings are kept for
13 months. **Deleting an organization removes its usage history, budgets and price overrides at
once**, regardless of retention. Removing a project or a member does not: their rows stay,
labelled with the name they had, until retention deletes them.

## Under the hood: the API

The page uses the portal's own API, which is not a stable public contract. For reference, all routes
need a signed-in session (in local mode, the portal's own page).

| Route | Returns |
|---|---|
| `GET /api/usage`, `GET /api/usage/calls`, `GET /api/usage.csv` | The organization's totals, daily series and breakdowns (`group` is `project`, `member`, `task`, `model`, `source`, `machine` or `day`), the call list and the export. Needs `org.usage` |
| `GET /api/usage/me`, `/api/usage/me/calls`, `/api/usage/me.csv` | The same, forced to the caller. Production |
| `GET /api/projects/{slug}/usage` | One project's totals and, in production, each member's share. Project admins |
| `GET /api/account/usage` | The caller's month in each organization. Production |
| `GET /api/budgets` and `PUT` / `DELETE` on `/api/budgets/org`, `/api/budgets/member-default`, `/api/budgets/members/{uid}`, `/api/projects/{slug}/budget` | Budgets, alerts, spend. Writes need an owner or admin |
| `GET`, `PUT`, `DELETE /api/prices` | The price table and overrides |

Filters are `from`, `to` (`YYYY-MM-DD`, `to` exclusive), `project`, `member` (a member id, or `system`),
`task`, `source`, `model`, `scan_run`, `chat_session`, `sort` (`time` or `cost`) and `before` (the
cursor `next` returned). A bad value is a `422` (`bad_date`, `bad_range`, `range_too_long`,
`bad_filter`, `bad_group`, `bad_sort`, `bad_cursor`). A report that takes too long answers **`503
usage_timeout`**: narrow the range and retry. Money is a JSON number in US dollars; token counts are
`null` when no row in the set reported them.
