# Platform administration

Platform administration is for the people who run a production WhyGraph instance. It lives at
`/admin` on the base host, with its own sign-in and its own session, apart from the organizations
people work in. It exists in [production mode](production.md) only; a local portal has no `/admin`
and answers `404` there.

A **platform administrator** (an instance admin) is a user who has been made an administrator. The first one is the
[bootstrap account](production.md#first-start); every later one is a GitHub user that an
administrator promotes.

## Admin mode

Being an instance admin does not make everyday sessions powerful. Administration uses a **separate
admin session**, started on purpose at `/admin/login`:

- **Two cookies, two jobs.** The everyday session (the one every organization host sees) never opens
  an `/api/admin` route, even when its user is an instance admin. The admin session is a second cookie,
  set on the base host only and never sent to an organization host. A page on an organization cannot
  read or use it.
- **8 hours, no idle limit.** An admin session ends 8 hours after sign-in, whatever you were doing.
  The sidebar shows when ("Session ends ..."). A save after that point answers that the session ended and
  takes you to the sign-in page, so an unsaved edit is lost.
- **It follows the account.** Removing someone's admin flag, disabling them or resetting their password
  ends their admin session on the next request. Signing in again as an administrator replaces your
  previous admin session in that browser.
- **Signing out of the portal ends it too.** An admin session started beside an everyday GitHub
  session ends when you sign out of that everyday session, from an organization host as well. An admin
  session in another browser is left alone.
- **It leaves the everyday session alone.** Signing in to administration does not sign you in to, or
  out of, your organizations. Use **Back to WhyGraph** in the sidebar to return to them.

- **Reading an organization is a grant.** An administrator is not a member of the organizations
  people work in, and the admin session does not let you into them. To read one, use **Open as
  reader** (see [Reading an organization](#reading-an-organization)).

## Signing in

Open `<your base URL>/admin`. Without an admin session you are sent to `/admin/login`, and back to
the page you asked for afterwards.

| Way | Who | How |
|---|---|---|
| **Continue with GitHub** | An instance admin with a GitHub account | The same GitHub sign-in as everyone else, including the required two-factor authentication. It only signs in an account that already exists and is an administrator: it never creates an account and never changes a username. Anyone else is refused with "not a platform administrator", and the refusal is written to the [event log](production.md#the-security-event-log). |
| **Sign in with a password** | The bootstrap account (and any password account that is an admin) | Email and password, behind a disclosure on the same page. This is the break-glass way in: a password account has no GitHub account behind it. |

A password account signs in **only here**. It cannot sign in to an organization, so it is for
administration and nothing else; the people who use organizations sign in with GitHub. After the
[bootstrap](production.md#first-start) and after a [password reset](production.md#instance-admins) you
land on `/admin`.

Keep at least two administrators, one of them a GitHub account. The last admin cannot be demoted, and
there is no recovery path for a locked-out one in this release.

## The modules

The nav on the left has six modules. Press `Cmd-K` (or `Ctrl-K`), or use **Search**, to jump to a page, a user
or an organization; a deleted one is marked **Deleted**.

| Module | What it shows |
|---|---|
| **Dashboard** | What needs attention, seven figures and three charts for a date range. See [Dashboard](#dashboard). |
| **Organizations** | Every organization, with its detail page and **Create organization**. See [Organizations](#organizations). |
| **Projects** | Every project of every organization, with its detail page. See [Projects](#projects). |
| **Users** | Every account, with its detail page. See [Users](#users). |
| **Audit** | The [security events](production.md#the-security-event-log) of every organization and the ones that belong to none. See [Audit](#audit). |
| **Configuration** | Who can sign up, who can create organizations, limits, the announcement banner and the sign-up allowlist, plus the instance's address, version and storage. See [Configuration](#configuration). |

The text on these pages is always shown as plain text, never rendered as markup, because the admin
session shares its origin with pages that show user-supplied names.

Every filter on a list is in the address, so a refresh, a shared link and the back button keep the
view. While any filter is set, **Clear filters** at the end of the filter row puts them all back at once.

### Dashboard

The page opens on the last 30 days. The date range at the top (This month, Last month, 7 / 30 / 90
days, 12 months or a custom range of at most 400 days) drives the three charts and the "since" figures
on the tiles, and is kept in the address (`?from=&to=`).

**Needs attention** lists what is broken or waits for an administrator, one line each, with a link
to the item: a scan that failed in the last 24 hours, a project that lost its GitHub access, an
organization, project or member that hit a hard-stopped budget, a problem the address check found (it links to [Configuration](#configuration)), a
newer WhyGraph release, a password account that owns organizations or cannot sign in, a suspended
organization, and a deleted organization or project that still holds data. With nothing to report it
says "Nothing needs your attention."

The seven tiles count live organizations, projects and users (deleted ones are left out):

| Tile | What it counts |
|---|---|
| Users, Organizations | The live total, and the change since the start of the range. |
| Projects | The live total. |
| LLM spend MTD | Estimated spend this month across every organization, and the same figure last month by the same day. |
| Tokens MTD | Input and output tokens this month. |
| Storage | The newest measurement of each project, deleted ones included, and the change since the start of the range. Purged projects are not counted. |
| Agent calls (7 d) | Calls by agents in the last 7 days, and the change on the 7 days before. Chat tool calls are not agent calls. |

The charts are LLM spend per day by provider (the five biggest providers, the rest as "other"), scans
per day by status, and the top 10 organizations by spend, storage or agent calls (the **Rank by**
select, kept in the address as `?rank=`). A figure that takes more than ten seconds to add up shows an
error in its own tile or chart with **Retry**, and the rest of the page stays.

### Projects

The list has an **Organization** filter, a **Show** filter (All, OK, Failing, Access lost, Never
scanned, Deleted) and a search on project name, project slug and organization slug, all kept in the
address. **All** holds every project that is not deleted. The columns are the project, its state, the
last scan, storage and spend this month.

A project's page shows its state, spend this month (against its budget), agent calls, the last scan
and the message of the last failure, then:

- **Rescan** queues a scan as a platform administrator would from the organization, and works while
  the organization is not suspended. **Measure now** measures its storage at once.
- **Delete**, **Restore** and **Delete permanently**, as on the organization's Projects tab. A
  deleted project shows only Restore and Delete permanently.
- **Scans**: the last 20 runs, each with its status, who requested it and its duration, **Cancel** on
  a queued or running one, and **Log**, the end of the run's log as plain text. The list refreshes
  every five seconds while a run is queued or running.
- **Storage**: the size of the history database, the Git data, the code index, the working tree and
  the run logs, and the total over the last 90 days.

#### Storage figures

Sizes are measured by walking each GitHub project's server copy and history on the data volume,
not by asking the database: the apparent size of the regular files, symbolic links not followed. The
portal measures a project when it has no measurement, when the last one is over 24 hours old or older
than its last scan, and checks every six hours. **Measure now** ignores those rules, and answers "Another
storage measurement is running" while the portal walks another project. Measurements are kept for 400
days, plus each project's newest one. Purging a project removes its measurements, so its bytes leave
the totals and the history charts for past days too.

### Users

The list has a **Show** filter (All, Admins, Disabled, Deleted, Password accounts) beside a search on
name, login and email. **All** holds every account that is not deleted; deleted ones appear under
**Deleted**. A password account that is not an administrator carries a **cannot sign in** badge: it can
sign in nowhere (see [Passwords](production.md#passwords)). **Owns** counts the organizations a user
owns, which is how you find the ones a password account must hand over. **Last session seen** is
accurate to about five minutes and is blank after a sign-out.

A user's page shows the profile, the organizations they belong to (each a link, with the role), their
sessions, their connection tokens, what they spent this month, and their latest events with a link to
**All events by this user**. The actions are:

- **Make admin** / **Remove admin** and **Disable** / **Enable**, as before. Neither Enable nor
  Make / Remove admin asks for a confirmation.
- **Copy reset link**, for a password account that is an administrator and not disabled (see below).
- **Revoke** on a session ends it at once; **Sign out everywhere** ends all of them, admin sessions
  included, but not the person's connection tokens. You cannot end the admin session you are using
  (use **Sign out**).
- **Revoke** on a connection token cuts that machine off from its project; the project's owner can
  connect it again. The organization sees this in its own audit log.
- **Delete**: see [Deleting, restoring and erasing](#deleting-restoring-and-erasing).

### Organizations

The list has a **Show** filter (All, Suspended, Deleted) beside a search on name and slug, and
columns for members, projects, owners, spend this month, storage, last activity and creation date. A suspended
organization stays in **All** with a **Suspended** badge. The address of an organization is plain text:
you are not a member, so its host answers "No access" until you [open it as a reader](#reading-an-organization).

**Create organization** takes a name, a URL name (filled in from the name) and the owner's GitHub
username. The owner must already have signed in to WhyGraph with GitHub: an administrator adds existing
accounts only, and anyone else is invited by an organization owner. A taken or invalid URL name comes
back on the form. Creating an organization ignores the per-person limit on how many one may own.

An organization's page has these sections, kept in the address (`?tab=`):

| Section | What it holds |
|---|---|
| **Overview** | Members, projects, open invitations and spend this month, the address and the stored rows (usage events, audit events, agent-call days). |
| **Members** | Every member with a role select that saves on change, **Remove**, and **Add member** by GitHub username. This is where you hand an organization over: promote a GitHub member to owner, then remove the password account. A password account cannot be made an owner. The last owner cannot be demoted or removed. |
| **Projects** | Its projects, deleted ones included, each linking to its [project page](#projects), with their state, last scan and storage. A live project has **Delete**; a deleted one has **Restore** and **Delete permanently**. |
| **Usage** | This month's spend, the budgets with their limits, hard stops and spend, and the storage of each project. |
| **Audit** | The organization's events, with the same filters and CSV as the Audit module. |

Under the heading, **Rename**, **Open as reader**, **Suspend** / **Unsuspend** and **Delete** act on
the organization. All of them except Open as reader work on a suspended one.

#### Suspending an organization

**Suspend** asks for a reason of up to 500 characters, which the members see. A suspended organization
is **read-only** for everyone: its data stays readable, and changes, scans, chat, agent calls, new
connections and imports stop. Running scans are cancelled, and a banner on every page says
"This organization is suspended: *reason*. Data is read-only." **Unsuspend** opens everything again.

What does not come back by itself: a scan or an import the suspension cancelled (an import needs
**Retry**; a project whose remote did not move needs a rescan), and connection tokens that sat unused
for 90 days during a long suspension, which were swept as idle and stay revoked.

#### Reading an organization

An administrator can read an organization only through a grant, never always. **Open as reader** on
the organization's page makes a grant for **you** that lasts 60 minutes and shows a link, "Open *org*
(read-only until *time*)", that opens the organization on its own host. The organization shows a banner,
"Viewing as a platform administrator, read-only until *time*", and every write is refused.

- There is no renewal: asking again while a grant is live returns the same one.
- **Account** lists your open grants with **Revoke**. A revoked or expired grant ends the access at
  once, and the organization then says "No access to this organization" with a button to the panel.
- Removing your admin flag, disabling you or deleting your account ends your grants.
- A password account cannot open one: it has no everyday session to carry it.
- Opening and revoking a grant are written to the event log.

### Audit

The **Audit** module shows every organization's events as well as the platform's own. An
**Organization** column names each event's organization, and the **Organization** filter picks All
organizations, **Platform events (no organization)**, or one organization (a deleted one is marked).
Filter by event, actor and dates; **Download CSV** saves the same view as `whygraph-admin-audit.csv`.
All filters are in the address.

### Deleting, restoring and erasing

Deleting hides, and nothing is lost until someone deletes **permanently**.

| What | Delete | Restore | Delete permanently |
|---|---|---|---|
| **An organization** | Type its URL name. It disappears for its members at once; scans stop and its connection tokens are revoked. Nothing is erased, and the name stays taken. | **Restore** brings it back. | Type its URL name. Its projects with their server copies and history, members' access, settings and keys are deleted, and the name can never be used again. |
| **A project** | Type its name. It disappears for everyone in its organization at once. The same button is what an organization owner or admin gets: for them removing a project is also a delete you can undo. | **Restore**, unless its organization is deleted (restore that first). | Type its name. Its server copy and history (descriptions, rationale cards, chats) are deleted. |
| **A user** | Type their GitHub login without the `@` (their email, or their ID, when they have none). They are signed out everywhere, their tokens are revoked and they leave their organizations. | **Restore** puts them back in the organizations that still exist. | **Erase permanently**, below. |

A repository whose project was deleted cannot be imported again until it is restored; the picker shows
it as removed.

**What a restore does not bring back.**

- An organization or project: connection tokens stay revoked, a linked local portal shows the project
  as removed until its user links it again, scans and imports the delete cancelled need a rescan or
  **Retry**, and a project that was deleted on its own stays deleted.
- A user: their administrator flag, their project access and their member budgets.

**The last-owner rule.** A user who is the only owner of an organization cannot be deleted. The error
names each such organization, deleted ones included (a deleted organization needs **Restore** and a
second owner, or **Delete permanently**). Hand the organization over first.

**`[Deleted User]`.** A deleted or erased user shows as `[Deleted User]` wherever an organization sees
them, in member lists, scan histories and audit entries, so a deleted person's name does not linger.
The admin panel shows the real name until the account is erased.

**Erase permanently** is for an account that is already deleted, and cannot be undone. It removes the
person's name, GitHub login, email, avatar, IP addresses, machine names, invitations and chats; the
history keeps an entry named `[Deleted User]`. It cannot reach log lines, backups or files already
exported. The same GitHub account can sign up again as a new account. If WhyGraph cannot open a
project's chat history, nothing is erased and the error names the project: fix or permanently delete
it, then erase again. If recent events are still being written, wait a minute and try again.

!!! tip "Keep a break-glass account"
    Use a password account with an email address that is not published anywhere else as your break-glass
    way in, and do not use it for anything else.

### Configuration

**Configuration** (title "Platform configuration") sets the policy for the whole platform. Each form has
its own **Save** and **Discard**, sends only the settings it holds, and takes effect at once without a
restart; the page says when it was last changed and by whom. A section you pick in the list beside the
page is kept in the address (`?section=signup`, `organizations`, `limits`, `announcement`, `allowlist`
or `instance`), so a refresh opens it again. Every change is written to the
[event log](production.md#the-security-event-log) (`instance_settings_changed`, with each setting's old
and new value). Nothing here is enforced in local mode.

| Section | What it sets |
|---|---|
| **Sign-up** | **Who can sign up**: anyone with a GitHub account, invited people and the allowlist, or nobody new. See [Who can sign up](production.md#sign-up). |
| **Organizations** | **Who can create organizations**: everyone, or platform administrators only. The "Create organization" buttons disappear for everyone else, and the page itself says only administrators can create one. |
| **Limits** | **Organizations one person may own** and **Projects per organization**: a whole number of 1 or more, or empty for no limit. A person at the limit gets "You already own ... organizations", and an import at the project limit stops, and says so on every repository it did not start. Instance administrators are not exempt on the everyday "Create organization" page. |
| **Announcement** | **Banner text** (up to 500 characters) and **Banner style** (Info or Warning), with a preview. |
| **Sign-up allowlist** | Shown while the saved policy is **Invited people and the allowlist**. See below. |
| **Instance** | Read-only: the address and its self-check, the version and update status, the GitHub App's slug and client id, the OAuth App's client id, the free space on the data volume (the database is on its own volume) and the database size. No secret is ever shown. |

#### Announcement

!!! warning "The banner is public"
    Everyone sees the text, **signed in or not**: it shows above the organization pages, on the sign-in
    and organization pages of the base host, and in the admin panel and its sign-in. Do not put anything
    in it that you would not post on the sign-in page. It cannot be closed by the viewer, and it is
    always shown as plain text, never as markup. Empty the text to remove it.

#### The sign-up allowlist

Under **Invited people and the allowlist**, an entry lets one GitHub account sign up, **once**. Add a
GitHub username and an optional note (up to 200 characters, for example "contractor, ACME"); the
portal asks GitHub for the account and stores its numeric id, so renaming the account on GitHub changes
nothing, and a login that someone else registers later is a different account and is refused. The
entry shows when it was added and by whom, and **Used** once the account has signed up; a used entry
admits nobody else. Adding someone who is already listed says so. **Remove** deletes an entry (the
person can still be admitted by an organization's invitation). Erasing a user also deletes the entries
for that GitHub account.

### Reset links

There is no email, so an administrator issues a password account's reset link and hands it over out of
band. It works once, for 24 hours, and its token sits in the URL fragment so no proxy log sees it.
Using it sets the new password, ends every session of that account and signs it in to administration.
A password account that is **not** an administrator gets no reset link (`not_an_admin`): promote it
first, or hand its organizations over and delete it (see [Upgrading](../portal/upgrading.md#admin-mode)).

## The Account page

**Account** (the signed-in admin in the sidebar) shows the account, when the admin session started and
when it ends, and your open [read-only grants](#reading-an-organization) with **Revoke**. A password account also changes its password here: **Current password**, **New
password** and **Confirm new password**. The password must be 15 to 256 characters and not a common
one (see [Passwords](production.md#passwords)). Changing it ends every other session of the account
and replaces this one.
