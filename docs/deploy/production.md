# Run in production

!!! note "Unreleased"
    Production mode is on `main` and is not in any released image yet; docs deploy from `main`. The
    compose file below pins the current release's image, which does not have it. Until a release ships
    it, build the image from the repository (`make image`) and point the compose file's portal image
    at it. This page describes the change on
    its way and may move until then.

Production mode runs the portal for **more than one person**: people sign in with GitHub (two-factor
authentication required), sessions live on the server, and every organization gets its own address,
`<org>.<your host>`. It is the same image and the same portal as [local mode](docker.md), selected by
`WHYGRAPH_MODE=production` at the first start; the mode can't be changed afterwards.

An organization's projects come **from GitHub**: its owners and admins import repositories through a
[GitHub App](#the-github-app) you create, the portal keeps its own copy of each one on the server,
and GitHub's push webhooks keep that copy current. Shared folders and local repositories are a
local-mode feature; see [what isn't there yet](#what-isnt-there-yet) for the rest.

## What you need

- A host with Docker and Docker Compose, reachable on ports 80 and 443.
- A **dedicated registrable domain** (or a domain you give to WhyGraph alone) for the base host.
- DNS you can edit, and an API token for the DNS host if you use the bundled Caddy build (Cloudflare
  in the example).
- A **GitHub OAuth App** for sign-in; see [GitHub sign-in](#github-sign-in).
- A **GitHub App** for importing projects; see [The GitHub App](#the-github-app). GitHub delivers its
  webhooks to the base host, so the host should be reachable from GitHub (see
  [Keeping projects current](#keeping-projects-current) for an instance that is not).

### Why a dedicated domain

The session cookie is set on the base host (`whygraph.example.com`) and is therefore sent to **every
subdomain** of it. That is what lets one sign-in cover every organization, and it has three
consequences:

- Everything under the base host must belong to WhyGraph. Another service on a sibling or parent
  host (`blog.example.com` beside `whygraph.example.com`) can set a cookie that reaches WhyGraph. The
  portal treats a request carrying two session cookies as signed out and clears both, but the clean
  fix is not to share the domain.
- HSTS is sent with `includeSubDomains`, so a browser that has seen it refuses plain HTTP for the
  whole host and everything under it. **Never add `preload`**, and never use an apex that other
  services share.
- Orgs are `<org>.<base host>`, so org names are public DNS-style labels.

## DNS

Two records point at the host that runs the compose bundle:

| Record | Name | Value |
|---|---|---|
| `A` / `AAAA` | `whygraph.example.com` | the host's address |
| `A` / `AAAA` | `*.whygraph.example.com` | the same address |

The portal checks on start that the base host and a random subdomain resolve, and warns when they
do not (see [the self-check](#the-self-check)).

## The wildcard certificate

Orgs appear as soon as someone creates them, so the certificate has to cover `*.whygraph.example.com`.
Let's Encrypt issues a wildcard only through the DNS-01 challenge, which needs a DNS plugin that stock
Caddy does not ship. The bundle builds Caddy with the Cloudflare plugin in
[`docker/production/caddy/Dockerfile`](https://github.com/mtrdesign/whygraph/blob/main/docker/production/caddy/Dockerfile),
and the Caddyfile is three lines:

```caddy
{$WHYGRAPH_HOST}, *.{$WHYGRAPH_HOST} {
	tls {
		dns cloudflare {env.CLOUDFLARE_API_TOKEN}
	}
	reverse_proxy portal:8765
}
```

Caddy forwards the `Host` header unchanged, and the portal's host check depends on that. If your DNS
is not on Cloudflare, swap the plugin in the Dockerfile and the `dns` line for your provider's. The
token needs permission to edit DNS records in the zone.

## GitHub sign-in

Everyone except the [bootstrap admin](#first-start) signs in with GitHub. You create one **OAuth App**
for your instance (one per base URL), under your own account or, so that an organization owns it,
under a GitHub organization's settings: Settings, Developer settings, OAuth Apps, New OAuth App.

| Field | Value |
|---|---|
| Application name | Anything, such as `WhyGraph (whygraph.example.com)`. Users see it on the consent screen. |
| Homepage URL | Your base URL, `https://whygraph.example.com` |
| Authorization callback URL | `<your base URL>/auth/github`. An OAuth App takes exactly one callback URL, so a second base URL needs a second app. |
| Enable Device Flow | Off |

Then choose **Generate a new client secret** and save it to a file (`secrets/github_oauth_client_secret`
in the compose bundle). The portal needs two values, and **both are required in production**: without
them the start stops with exit code `2` and a message naming what is missing.

| Variable | Value |
|---|---|
| `WHYGRAPH_GITHUB_OAUTH_CLIENT_ID` | The app's client ID. |
| `WHYGRAPH_GITHUB_OAUTH_CLIENT_SECRET_FILE` | The path of the file holding the client secret (trailing whitespace is stripped; an unreadable or empty file is refused). |

GitHub does not review an OAuth App. What the consent screen asks for is **read-only access to the
user's profile** (the `read:user` scope) and nothing else; WhyGraph refuses a sign-in that was granted
any other scope. The portal checks **GitHub's two-factor flag** and refuses an account that has not
turned two-factor authentication on, with a link to GitHub's security settings.

What the portal keeps of a GitHub user is the numeric GitHub id (the identity: a renamed account stays
the same user), the current username, the display name and the avatar address. **No email is stored.**
GitHub's access token is used once to read the user and is revoked straight away; the portal keeps no
GitHub token. A failed revoke is written to the [event log](#the-security-event-log). If someone
takes a username that another account used to have, the older account loses that username at the new
owner's next sign-in. The sign-in address (`/auth/github`) carries a one-time code, so the portal
redacts its query string from the access log.

**GitHub Enterprise Server.** Set `WHYGRAPH_GITHUB_URL` to the server's address (default
`https://github.com`) and `WHYGRAPH_GITHUB_API_URL` to its API (default `https://api.github.com`; on
Enterprise Server it is `<url>/api/v3`). `http` is accepted only for `localhost` and loopback
addresses. Both apps use the same two addresses, and git clones and fetches from
`WHYGRAPH_GITHUB_URL` too. The pull request and issue crawl still works only for `github.com`: on
Enterprise Server a scan reads the git history and skips that crawl.

!!! note "Two apps, two jobs"
    The OAuth App only proves who someone is and never gets repository access. Repositories are read
    through a separate [GitHub App](#the-github-app), which the people who own them install and scope
    to the repositories they choose.

## The GitHub App

Projects are imported through **one GitHub App** for your instance. You create it once; every
organization on your instance uses it, and the GitHub accounts and organizations that own the
repositories install it. Create it under your own account or a GitHub organization: Settings,
Developer settings, GitHub Apps, New GitHub App.

| Setting | Value |
|---|---|
| GitHub App name | Anything; its URL name (`github.com/apps/<slug>`) is the **slug** the portal needs. |
| Homepage URL | Your base URL, `https://whygraph.example.com` |
| Callback URL | `<your base URL>/auth/github-app`, as the **first** callback URL (GitHub sends an installation back to the first one). |
| Expire user authorization tokens | On |
| Request user authorization (OAuth) during installation | On |
| Redirect on update | On, so configuring the installation returns to the portal too |
| Webhook | Active, URL `<your base URL>/github/webhook`, with a secret of **at least 32 characters** (`openssl rand -hex 32`) |
| Repository permissions | **Contents**, **Metadata**, **Pull requests** and **Issues**, all **read-only**. Nothing else, and no account permissions |
| Subscribe to events | **Push** and **Repository**. GitHub always sends the installation events, which the portal uses too |
| Where can this GitHub App be installed? | **Any account**, so that people can install it on their own GitHub organizations |

Then, on the app's page, **generate a client secret** and **generate a private key** (GitHub downloads
a `.pem` file). The portal needs five values, and **all five are required in production**: without
them the start stops with exit code `2` and a message naming each one that is missing. The files are
read once at start; the private key must be an unencrypted RSA key, and a webhook secret shorter than
32 characters is refused.

| Variable | Value |
|---|---|
| `WHYGRAPH_GITHUB_APP_SLUG` | The app's URL name. |
| `WHYGRAPH_GITHUB_APP_CLIENT_ID` | The app's client ID (not its numeric app ID). |
| `WHYGRAPH_GITHUB_APP_CLIENT_SECRET_FILE` | The path of a file holding the client secret. |
| `WHYGRAPH_GITHUB_APP_PRIVATE_KEY_FILE` | The path of the private key's `.pem` file. |
| `WHYGRAPH_GITHUB_APP_WEBHOOK_SECRET_FILE` | The path of a file holding the webhook secret. |

**The private key never leaves the portal process.** For each fetch the portal asks GitHub for an
installation token **scoped to the one repository** and to read-only access, valid for at most an
hour. A scan gets that token through a file only it can read, and nothing else in the instance
holds it. See the [security model](../portal/security.md#production-mode).

## The compose bundle

The bundle is in
[`docker/production/`](https://github.com/mtrdesign/whygraph/tree/main/docker/production): three
services on one private network.

| Service | What it is |
|---|---|
| `postgres` | The portal's database, on a named volume at `/var/lib/postgresql`, with no published port. |
| `portal` | `whygraph portal --host 0.0.0.0 --port 8765` in production mode, no published port, with a named volume at `/data`. |
| `caddy` | The TLS front door on ports 80 and 443, at the fixed address `172.30.0.10`. |

Set it up:

```bash
cd docker/production
cp .env.example .env              # then edit it
mkdir -p secrets
openssl rand -base64 32 > secrets/postgres_password
chmod 600 secrets/postgres_password
# the OAuth App's client secret, from GitHub (see "GitHub sign-in" above)
printf '%s' 'the-client-secret' > secrets/github_oauth_client_secret
# the GitHub App's client secret, private key and webhook secret (see "The GitHub App" above)
printf '%s' 'the-app-client-secret' > secrets/github_app_client_secret
cp /path/to/the-downloaded.private-key.pem secrets/github_app_private_key.pem
printf '%s' 'the-webhook-secret' > secrets/github_app_webhook_secret   # the one set on the app
chmod 600 secrets/*
docker compose up -d --build
```

`.env` holds six values: `WHYGRAPH_BASE_URL` (for example `https://whygraph.example.com`, scheme and
host, no path), `WHYGRAPH_HOST` (the same host without the scheme), `CLOUDFLARE_API_TOKEN`,
`WHYGRAPH_GITHUB_OAUTH_CLIENT_ID`, `WHYGRAPH_GITHUB_APP_SLUG` and `WHYGRAPH_GITHUB_APP_CLIENT_ID`.
`secrets/` holds five files: `postgres_password`, `github_oauth_client_secret`,
`github_app_client_secret`, `github_app_private_key.pem` and `github_app_webhook_secret`. `.env` and
`secrets/` are gitignored. Every secret is handed to the containers that need it as a read-only file
(`/run/secrets/...`, named by `WHYGRAPH_DATABASE_PASSWORD_FILE` and the `..._FILE` GitHub variables)
and never as an environment value. The bundle sets the GitHub variables for the default `github.com`;
for Enterprise Server add `WHYGRAPH_GITHUB_URL` and `WHYGRAPH_GITHUB_API_URL` to the portal's
environment.

The base URL is the one value the portal and the browser must agree on, because an exact `Origin`
comparison is made against it. Use `https` in production; plain `http` is accepted only for hosts
ending in `.localhost`. A default port (`:443`) is dropped.

## First start

Production starts with no users. While no instance admin exists, the portal prints a one-time
**bootstrap secret** in its log:

```bash
docker compose logs portal | grep "Bootstrap secret:"
```

```text
First-time setup: open https://whygraph.example.com/setup and enter the bootstrap secret below.
Bootstrap secret: <24 characters>
```

Open `<your base URL>/setup`, enter the secret, an email, a name and a password. That creates the
first **instance admin** and signs you in; you land on "Create organization". The secret exists only in
the portal's memory, is regenerated on every start until an admin exists, and the route is inert once
one does. Until it is used, sign-in answers that the instance is not claimed yet, so nobody can grab
org names first. The bootstrap admin is the one account with an email and a password; everyone else
signs in with GitHub, and later admins are GitHub users that an admin promotes from the admin page.

## Open sign-up

!!! warning "Anyone with a GitHub account that has two-factor authentication can sign in and create organizations"
    Sign-up is always open in this release, and **it cannot be closed yet**. The first GitHub sign-in
    creates the account, and any signed-in user can create an organization. Each organization reserves
    a subdomain, and there is no cap. GitHub's own checks and the required two-factor authentication
    are what stands in the way, and sign-in bursts are throttled. An instance admin can
    [disable](#instance-admins) an abusive account.

    Until a later release adds a way to close it, **restrict network access to the base host** - a
    VPN, or an IP allowlist in Caddy - unless you want strangers on it.

No email is sent or verified, because nobody signs in with one.

## Passwords

Only the bootstrap admin has a password (and any password account carried over from an earlier
build). It must be **15 to 256 characters**, must not be on a list of common passwords, and must not
be the account's email or its local part. There are no composition rules; a passphrase works well.
Passwords are hashed with argon2, and repeated failed sign-ins are throttled per account and address.
A GitHub account has no password: the password form, change and reset link do not apply to it.

## Members

An organization's people are its **members**, each an `owner`, `admin` or `member`. People are added by
**exact GitHub username**, with no search and no autocomplete. If that person has already signed in to
this instance, they are added at once and see the organization in their picker. If not, they get an
**invitation** instead, and the organization shows them as pending (see [Invitations](#invitations)).
A username that is not a GitHub user (an organization's login counts) is refused with "No one with that
GitHub username exists". Adding is throttled to 60 per hour per organization.

### Organization roles

| Who | Can |
|---|---|
| **Member** | Read the organization. See **their own** [usage](../portal/usage.md#who-triggered-a-call) and budget, and nobody else's. What a member can do on each project comes from their [project role](#project-roles). |
| **Admin** | Everything a member can, plus import and remove projects, manage people (add members and admins, change their roles, remove them), and administer every project. See the organization's **Usage & cost**, everyone's spend included, and set [budgets](../portal/usage.md#budgets-and-the-hard-stop) and the price table. Cannot make or touch an owner, and cannot set or remove their own or an owner's per-member budget. |
| **Owner** | Everything an admin can, plus make or demote an owner, remove an owner, change the organization's settings and organization-level keys, read the [audit page](#the-security-event-log), transfer ownership and [delete the organization](#deleting-an-organization). Owners can set anyone's per-member budget. |

Admins and owners are admins of every project in the organization, always, and cannot be Restricted out
of one.

### Project roles

A member's role on a project is the one granted to them on it, or else the organization's default (see
[Restricted projects and the default](#restricted-projects-and-the-default)).

| | Viewer | Contributor | Admin |
|---|---|---|---|
| See the project, its overview and scan history | yes | yes | yes |
| Explorer: graph, evidence, history and **existing** rationale cards | yes | yes | yes |
| Connect agents ("Use with your agent", linked local portals) | yes | yes | yes |
| Explorer: generate a **new** rationale card | - | yes | yes |
| Chat | - | yes | yes |
| Quick rescan (git and CodeGraph, no LLM) | - | yes | yes |
| Full rescan and Describe now (LLM) | - | - | yes |
| Settings, keys and the project's access list | - | - | yes |
| The project's **usage** and its budget: this month's spend, each person's share, the project budget | - | - | yes |

A viewer never causes LLM spend: no chat, no new rationale card and no lazy description backfill,
whether they use the Explorer, a connected portal or an agent.

### Usage and budgets

Every LLM call the portal makes is recorded with who triggered it, so an organization can see what it
spends and cap it. The full picture is on [Usage & cost](../portal/usage.md); what matters for roles
is this:

- **Owners and admins see everyone.** The organization's Usage & cost page has a Members tab, a
  drill-down per person and a "this month" figure beside each person on the Members page and in a
  project's Access section. **A member sees only their own** spend, on **My usage** (Account page
  and the Usage & cost item). The portal never returns chat content or prompt text through any of it:
  counts, cost and a short subject such as a commit hash or a file path.
- **A project admin sees that project's usage** in its Settings, under **Usage**: the month's spend,
  its budget and each person's share of it.
- **Instance admins** reading an organization see its usage too, read-only, like everything else
  they read.
- **Budgets** can be set per organization, per project, per member (an organization-wide default plus
  personal overrides) and every one must be at or below the organization's budget. With the **hard
  stop** on, a used-up budget makes whoever it covers behave like a Viewer for LLM spend until the
  month resets or the budget is raised: no chat, no new rationale card, no full scan. A member's
  budget covers that member everywhere; a project's covers everyone on that project; the
  organization's covers everyone, owners included. Reading and quick scans keep working.
- A **Viewer** and a member whose hard stop engaged are different: the role is permanent, the stop
  lifts itself. When both apply, the role is what the notice says.

### Restricted projects and the default

The organization's **default project role** (`contributor`, `viewer` or `none`) is what a member gets on
a project they have no grant on. The owner sets it under the organization's settings. A project's admin
can mark the project **Restricted**: then only admins, owners and people with an explicit grant can see
it, whatever the default. A project a person cannot see is not found for them (404), in lists, in
URLs and over the API. Grants are managed on the project's **Access** tab. Promoting someone to admin or
owner drops their grants, since the role already covers every project. When a person loses access (the
project turns Restricted, the default drops to `none`, a grant is removed or the person is demoted),
their [connection tokens](#connected-portals) for that project are revoked at once.

### Invitations

An invitation names a GitHub username, an organization role and, optionally, project grants (not for
admins and owners). **No mail is sent**: tell the person to sign in to this instance with that GitHub
account. On their next sign-in the invitation is redeemed, they become a member and the grants are
applied. An invitation is valid for 14 days and there is one open invitation per person per
organization. Admins can revoke it from the members page, and only owners can revoke an owner
invitation.

### Ownership transfer and renaming

- **Transfer.** An owner can make another member the owner and become an admin in one step, by typing
  the organization's slug to confirm.
- **Rename.** An owner can change the organization's display name. The slug (and so the host) never
  changes.

### Other rules

- **The last owner is protected.** An organization's final owner cannot be demoted, removed or leave.
- **Leaving.** Anyone can leave an organization from its members page. Someone added against their will
  leaves in one click.
- **Removing** a member does not delete what they wrote: their chat sessions are kept but become
  invisible to everyone, and adding the person back restores them. Chat sessions are private to the
  user who started them.
- **Disabling.** An instance admin can disable a user from the admin page. That ends every session of
  the user at once and refuses their sign-in and any reset link; memberships are kept, and enabling
  the account restores access. You cannot disable your own account, and the last enabled admin can be
  neither demoted nor disabled. A scan-event stream a person is following ends when their access is
  removed.

## Instance admins

An instance admin manages the instance from `/admin` on the base host: users, who is an admin, the
list of organizations and the [self-check](#the-self-check).

- **Keep two admins.** The last admin can't be demoted, and there is no recovery path for a locked-out
  admin in this release.
- **Reset links.** There is no email, so an admin issues a one-time **reset link** for a password
  account (valid 24 hours) and hands it over out of band. GitHub accounts have no password, so they
  get none. The token is in the URL fragment, so it never reaches a
  proxy log. Using it sets the new password and signs out every session of that user.
- **Read access to every organization.** An admin who is not a member of an organization can still
  open it, **read-only** (GET requests only), under a "Viewing as instance admin" banner. Each such
  request is written to the security event log. These reads never trigger the lazy description
  backfill, so an admin's reading spends none of the organization's LLM keys.
- **There is no admin mode.** Admin powers are always on for the session, so a stolen admin cookie
  gives up to 30 days of reading every organization and issuing reset links. Protect admin accounts:
  a long unique passphrase, a trusted device, and the event log below.

Sessions last 30 days at most and expire after 7 days idle.

## Projects

A production project is a repository on GitHub. The portal keeps its own copy under the data
directory, at `repos/<org>/<project>`, scans its **default branch**, and keeps the project's WhyGraph
and CodeGraph databases in that copy. Nothing is ever written to the repository on GitHub.

### Importing a repository

Owners and admins choose **Import from GitHub** on the Projects page. The wizard has three steps:
**Source**, **Configure** and **First scan**. There is no Initialize step: the import itself sets
the project up, and a server copy gets no agent files, git hooks or marker files.

1. **Connect GitHub.** The first time, the page sends you to GitHub to authorize the app for your
   account. It must be the **same GitHub account you signed in with**; another one is refused. The
   portal keeps that authorization in memory for your session (at most 8 hours), never in the
   database; after a restart of the portal you pass through GitHub again.
2. **Pick an installation.** The page lists the app's installations you can see on GitHub. **Install
   / configure on GitHub** installs the app on another account or organization, or changes which
   repositories an installation covers, and brings you back.
3. **Pick a repository.** Repositories load 100 at a time (**Load more**), with a filter over the
   loaded ones; those already in this organization are disabled.

An import succeeds only if, at that moment, **you** can see the installation and read the
repository on GitHub, and the installation covers it. An installation is not tied to one WhyGraph
organization: whoever can see a repository through it can import it into an organization they
administer, and the same repository can be a project in two organizations, each with its own copy.
Within one organization a repository can be imported once. Imports are throttled to 30 an hour per
organization. The bootstrap admin signs in with a password, not GitHub, so it cannot import.

A production project's settings show its repository and installation account read-only. Its
remote, default branch and hooks cannot be changed, and a personal access token is refused (the
GitHub key is a local-mode setting): every GitHub call uses the app's installation token. The pull
request and issue crawl can still be switched off.

Installing the app from GitHub's own pages works too, but the redirect that follows asks you to
start from your organization in WhyGraph and choose Import from GitHub.

### Keeping projects current

- **A push** to a project's default branch reaches the portal as a webhook and queues a fetch and a
  scan within seconds. Pushes to other branches and tags are ignored, and a burst of pushes becomes
  one follow-up run.
- **Every hour**, and when the portal starts, it compares each project's default branch on GitHub
  with the last scanned commit and fetches when it moved. That catches the pushes whose webhooks
  were missed while the portal was down.
- **Scan now** always fetches first, then scans.

A fetch follows a renamed or transferred repository (a project is tracked by GitHub's numeric
repository id, never by its name, so a reused name never brings in someone else's history), a new
default branch, and a force-push: the copy is reset to GitHub's branch, and the run notes that the
history was rewritten.

An instance that GitHub cannot reach still works, at the pace of the hourly check plus **Scan now**.

### When access is lost

When GitHub stops letting the app read a repository, the project shows an **Access lost** badge with
the reason:

| Reason | What happened |
|---|---|
| The WhyGraph app can no longer read this repository | The app was uninstalled or suspended, or the installation no longer includes the repository (or GitHub refused a token for it). |
| GitHub refused git access to this repository | A fetch with a fresh token was refused. |
| The repository was deleted on GitHub | GitHub reported the repository deleted. |

The Explorer and Chat keep serving what was already scanned; scans are refused until access
returns. Owners and admins get **Reconnect on GitHub**, the app's install / configure page. The badge
**clears itself**: at once when the app is installed again or the repository is added back to the
installation, otherwise at the next hourly check that gets a token for the repository (which also
brings back a repository restored on GitHub). A project is **never removed automatically**; its
history is evidence. **Remove** works as usual.

### Repositories that track WhyGraph's state

A repository that commits `.whygraph/` or `.codegraph/` is refused at import: those folders hold the
databases the portal builds and serves to every member, and a committed copy would be served as if
the portal had built it. If the default branch starts tracking them later, the next fetch fails and
the project is marked "The default branch tracks .whygraph/ or .codegraph/". Remove them from the
repository and push; the next fetch checks again and clears the mark.

### Removing a project

Only organization admins and owners remove a project (a project admin who is only a project role holder cannot), from its settings, by typing the project's name. That deletes the
server copy - with its history, descriptions, rationale cards and chat sessions - and every scan of
it. The repository on GitHub is not touched; importing it again starts from scratch.

## Connected portals

People on your team can link a checkout on their own machine to a project here, so their coding
agent reads the project's history through their **local** WhyGraph, with their uncommitted work kept
on the laptop. Nothing about this needs setup beyond a working production portal: the project's home
page has a **Use with your agent** panel that walks a member through it, and the member-side steps
are in [Projects from a platform](../portal/platform-projects.md).

Each link is a **connection token**: one project, one person, with that person's role re-checked on
every call. A member **allows** it on a consent page on the base host, signed in as themselves. Their
local portal then calls this one over `/api/v1`, on the organization's host, with the token as a
bearer. Only members can connect: an instance admin's read-only access to an organization cannot.

There are two lists, both called **Connected portals**:

- **Your own**, on your Account page: every portal you have linked, across projects, with the
  machine name, when it was linked and when it was last used. You can revoke any of them.
- **A project's**, in its settings, for **owners and admins**: every member's connection to that
  project, with the **machine name each member gave it**, their name and when it was last used. An
  admin can revoke any of them, which covers a lost laptop whose owner is unreachable (the reason is
  recorded as `admin_revoked`). Members' machine names are therefore visible to the project's admins.

A revoked token stays in the lists for 30 days, with its reason, then is deleted.

Revocation follows membership, in the same transaction:

| Event | What happens to tokens |
|---|---|
| A member is removed, or leaves | Their tokens for that organization are revoked |
| An instance admin disables an account | All of that person's tokens are revoked |
| A project is removed, or an organization deleted | Its tokens are revoked |
| The member removes the project from their machine | That one token is revoked |
| A token is unused for 90 days, or never used within an hour | It expires |

**Re-enabling a disabled account, or adding a member back, does not restore any token.** The person
links again. A removed member's token is also refused the moment they are not a member, whether or
not the revocation had run.

Connections are written to the [event log](#the-security-event-log) (`connection_authorized`,
`connection_token_issued`, `connection_token_refused`, `connection_revoked`), never with a token or a
code.

### Agent limits

An agent on a connected portal can cause work here: a rationale card nobody has generated yet, and
the lazy description of commits that have none. Both spend the organization's own model keys, so an
**owner** bounds them in the organization's **Settings**, under **Agent limits**:

| Setting | Default | Counts |
|---|---|---|
| `[rationale].agent_generations_per_hour` | `120` | Rationale cards generated for agents, per hour, across the organization |
| `[analyze].agent_descriptions_per_hour` | `600` | Commits described for agents' evidence, per hour, across the organization |

Each also has a **per-member** limit, so one person's agents cannot use the whole allowance:

| Setting | Default | Counts |
|---|---|---|
| `[rationale].agent_generations_per_member_per_hour` | `30` | Rationale cards generated for **one member's** agents, per hour |
| `[analyze].agent_descriptions_per_member_per_hour` | `150` | Commits described for **one member's** agents, per hour |

Every call must fit under both its member limit and the organization limit, which stays the ceiling
for everyone together.

`0` means agents get only what already exists: cached cards and existing descriptions. A card
already in the cache is always served, and it is shared by every member and by the Explorer, so each
is paid for once. When a limit is reached an agent is told so (`429 generation_limited`, or `403
generation_disabled` for `0`), and the answer carries a `scope` of `org` or `member` saying which
limit it was; a missing rationale key is `409 no_llm_key`. A [budget](../portal/usage.md#budgets-and-the-hard-stop)
that is used up with the hard stop on answers `403 budget_exceeded`, also with a `scope`. These four keys exist
**only** as organization defaults: they are never set per project and never imported from a
repository. See [Configuration](../reference/configuration.md#agent-limits-organization-only).

Beyond the limits, each token is rate limited, at most two evidence or rationale requests run at
once per organization (a third is answered `503 busy` rather than queued), and one request does a
bounded amount of `git` work.

## Deleting an organization

An **owner** deletes an organization from its **Settings** page (the danger zone), by typing the
organization's slug. It is **immediate**, with no grace period and no undo: running scans are
cancelled, then the projects, their server copies and scan files, the memberships, the settings, the
keys and the [usage history and budgets](../portal/usage.md#retention-and-deletion) go in one operation. **The slug is retired forever** - nobody can create an organization
with it again. Members keep their accounts and sessions, and the organization's address stops
answering.

If a fetch is still running, the deletion stops with "A sync is finishing - try again in a minute"
and nothing is deleted. An instance admin who is not an owner cannot delete an organization.

## The security event log

The portal writes one structured `INFO` record on the `whygraph.portal.audit` logger for each:
bootstrap claimed, password sign-in (success and failure), sign-out, password change, reset link
issued and used, admin granted or revoked, organization created, and instance-admin read request,
plus these GitHub, member, project and organization events:

| Event | When |
|---|---|
| `github_signin` | A GitHub sign-in succeeded (and whether the account is new). |
| `github_signin_refused` | It was refused: the `reason` is `oauth_state`, `exchange_failed`, `scope`, `unavailable`, `2fa_required` or `disabled`. |
| `github_token_revoke_failed` | GitHub's one-time token could not be revoked. |
| `github_login_released` | A username moved to another account and was taken from the older one. |
| `member_added`, `member_add_refused` | A member was added, or the attempt was refused (with the reason and the username tried). |
| `member_role_changed`, `member_removed`, `member_left` | A role changed, or someone was removed or left. |
| `invitation_created`, `invitation_revoked`, `member_joined_by_invite` | An invitation was made or revoked, or redeemed at sign-in. |
| `project_grant_added`, `project_grant_changed`, `project_grant_removed` | A project grant changed. |
| `project_restricted_changed` | A project was marked or unmarked Restricted. |
| `org_renamed`, `org_default_role_changed`, `org_ownership_transferred` | An owner renamed the organization, changed its default project role or transferred ownership. |
| `user_disabled`, `user_enabled` | An instance admin disabled or enabled an account. |
| `github_app_authorized` | Someone authorized the GitHub App, or installed it, from the import page. |
| `github_account_mismatch` | That authorization was for a different GitHub account than the signed-in one, and was refused. |
| `project_imported`, `project_removed` | A repository was imported, or a project was removed. |
| `connection_authorized`, `connection_token_issued`, `connection_token_refused`, `connection_revoked` | A member allowed a connected portal, its token was issued, a token exchange was refused, or a token was revoked (with the reason). |
| `project_access_lost`, `project_access_restored` | A project lost its GitHub access (with the reason), or got it back. |
| `webhook_rejected` | A webhook delivery had a missing or wrong signature. |
| `org_deleted` | An owner deleted an organization (with its project count). |
| `budget_threshold_crossed` | A budget passed 50, 75 or 100% of its month, once per threshold per month (with the `scope` - `org`, `project` or `member` - the `threshold`, the spend and the budget, and the member or project it names). |
| `budget_hard_stop_engaged` | A budget with the hard stop reached 100% (same fields): LLM spend it covers is off until the month resets or the budget is raised. |
| `budget_set`, `budget_removed` | An owner or admin set or removed a budget (with its scope, amount and hard-stop flag, and the member or project). |
| `price_override_set`, `price_override_removed` | An owner or admin set or reverted an organization price for a provider and model. |

Each carries the event, the user, the target user, the client address and the host. A password
sign-in failure shows only the first 3 characters of the email and its domain. No token, code, state,
secret or password is ever logged or stored: every value passes through token redaction first.

They go to the portal's normal log (`docker compose logs portal`) and, in production, **also to the
portal's database**, where they are kept for **400 days** and then deleted.

- **The audit page.** Owners see an **Audit** page for their organization, filterable by event, person
  and date, 50 events to a page. **Download CSV** exports the same filter (at most 50,000 rows). A cell
  that starts with `=`, `+`, `-` or `@` is prefixed with `'` so a spreadsheet does not run it.
- **Instance admins** have the same list on `/admin` for events that belong to no organization and for
  organizations that were deleted.
- Rows record **attempts**, so a refused action is there too. A busy portal drops rows rather than
  slowing requests (and logs a warning); the log line is always written.
- An instance admin's reads are recorded at most once per hour per organization.

## What isn't there yet

- **Email**: no verification and no reset mail (GitHub sign-in needs none). An invitation sends no mail either: the person has to be told to sign in.
- **Agents and MCP**: there is no `/mcp` endpoint in production, and a server copy gets no agent
  files or git hooks. Agents reach a production portal's projects through a developer's
  [connected portal](#connected-portals).
- **Projects from anywhere but GitHub**: shared folders and local repositories are refused, and
  only the default branch is scanned. On GitHub Enterprise Server the pull request and issue crawl
  is skipped.
- **Closing sign-up** (see above).
- **Other sign-in providers.** GitHub is the only one, and its two-factor authentication is required;
  there is no GitHub-less sign-in for end users and no multi-factor step for the bootstrap password
  account.
- **Recovery of a locked-out admin**, **deleting** a user, and a list of your sessions.

## Trusted proxies

Behind Caddy every request reaches the portal from Caddy's address, so the portal needs to be told
whose `X-Forwarded-For` to believe, or all clients share one address and one throttle.
`WHYGRAPH_TRUSTED_PROXIES` takes a comma-separated list of IPs or CIDRs; the compose file sets it to
Caddy's fixed address, `172.30.0.10`, which is why the network has a fixed subnet. Unset means trust
none, and a bad entry stops the start with exit code 2. If you put another proxy in front of Caddy,
add its address too. Behind a proxy that is not listed, the portal logs one warning the first time a
request carries `X-Forwarded-For`.

## The self-check

On start the portal looks up the base host and a random subdomain of it. It never stops the start; a
missing record is a warning, written to the log and shown on the admin page, naming the record that
is missing (usually the wildcard). It is skipped for `*.localhost`.

## Backups

Two things hold the instance's state, and both must be backed up:

- **The database** - the compose `postgres` volume: users, sessions, organizations, settings and
  encrypted keys. See [Backup and restore](../portal/backup.md) for dumping and restoring.
- **`/data` in the portal container** (the `portal-data` volume). It holds **`secret.key`**, the key to
  every stored secret: a database dump without it is unreadable for the secrets, and without it the
  keys have to be entered again. It also holds the projects' server copies (`repos/`), and with them
  each project's WhyGraph database: its descriptions, rationale cards and chat sessions. Those cannot
  be fetched from GitHub again; a re-import starts from scratch.

Back up `secret.key` together with each dump, and treat the pair as sensitive as the live data.

## Security headers and hosts

What the portal does differently in production is on the
[security model](../portal/security.md#production-mode) page.
