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

This release gets you identity and organizations. Production organizations hold **no projects yet** -
see [what isn't there yet](#what-isnt-there-yet).

## What you need

- A host with Docker and Docker Compose, reachable on ports 80 and 443.
- A **dedicated registrable domain** (or a domain you give to WhyGraph alone) for the base host.
- DNS you can edit, and an API token for the DNS host if you use the bundled Caddy build (Cloudflare
  in the example).
- A **GitHub OAuth App** for sign-in; see [GitHub sign-in](#github-sign-in).

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
addresses.

!!! note "Importing projects will use a second app later"
    The OAuth App only proves who someone is. Reading repositories, when production gains projects,
    will use a separate GitHub App that an organization owner installs and scopes to the repositories
    they choose. The sign-in app never gets repository access.

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
chmod 600 secrets/github_oauth_client_secret
docker compose up -d --build
```

`.env` holds four values: `WHYGRAPH_BASE_URL` (for example `https://whygraph.example.com`, scheme and
host, no path), `WHYGRAPH_HOST` (the same host without the scheme), `CLOUDFLARE_API_TOKEN` and
`WHYGRAPH_GITHUB_OAUTH_CLIENT_ID`. `secrets/` holds two files: `postgres_password` and
`github_oauth_client_secret`. `.env` and `secrets/` are gitignored. Both secrets are handed to the
containers that need them as files (`/run/secrets/...`, named by `WHYGRAPH_DATABASE_PASSWORD_FILE` and
`WHYGRAPH_GITHUB_OAUTH_CLIENT_SECRET_FILE`) and never as environment values. The bundle sets the
GitHub variables for the default `github.com`; for Enterprise Server add `WHYGRAPH_GITHUB_URL` and
`WHYGRAPH_GITHUB_API_URL` to the portal's environment.

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
**exact GitHub username**, with no search and no autocomplete, and only if that person has **already
signed in to this instance once**; otherwise the answer is "No one with that GitHub username has signed
in to WhyGraph yet", so ask them to sign in first. Adding is direct: there is no invitation and no
acceptance step, and the person sees the organization in their picker at once. Adding is throttled to
60 per hour per organization.

| Who | Can |
|---|---|
| **Member** | Read the organization and its projects. |
| **Admin** | Everything a member can, plus manage people - add members and admins, change their roles and remove them - and the per-project settings and keys. Cannot make or touch an owner. |
| **Owner** | Everything an admin can, plus make or demote an owner, remove an owner, and change the organization's settings and organization-level keys. |

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
  request is written to the security event log. Explorer views can still trigger the lazy backfill
  with that organization's keys, exactly as a member's views do.
- **There is no admin mode.** Admin powers are always on for the session, so a stolen admin cookie
  gives up to 30 days of reading every organization and issuing reset links. Protect admin accounts:
  a long unique passphrase, a trusted device, and the event log below.

Sessions last 30 days at most and expire after 7 days idle.

## The security event log

The portal writes one structured `INFO` record on the `whygraph.portal.audit` logger for each:
bootstrap claimed, password sign-in (success and failure), sign-out, password change, reset link
issued and used, admin granted or revoked, organization created, and instance-admin read request,
plus these GitHub and member events:

| Event | When |
|---|---|
| `github_signin` | A GitHub sign-in succeeded (and whether the account is new). |
| `github_signin_refused` | It was refused: the `reason` is `oauth_state`, `exchange_failed`, `scope`, `unavailable`, `2fa_required` or `disabled`. |
| `github_token_revoke_failed` | GitHub's one-time token could not be revoked. |
| `github_login_released` | A username moved to another account and was taken from the older one. |
| `member_added`, `member_add_refused` | A member was added, or the attempt was refused (with the reason and the username tried). |
| `member_role_changed`, `member_removed`, `member_left` | A role changed, or someone was removed or left. |
| `user_disabled`, `user_enabled` | An instance admin disabled or enabled an account. |

Each carries the event, the user, the target user, the client address and the host. A password
sign-in failure shows only the first 3 characters of the email and its domain. No token, code, state,
secret or password is ever logged.

They go to the portal's normal log: `docker compose logs portal`. A persistent audit table is not part
of this release, so ship the log somewhere if you need history.

## What isn't there yet

- **Email**: no verification and no reset mail (GitHub sign-in needs none), and no invitations: members are added directly.
- **Projects**: production organizations hold none. Adding a project, shared folders and the
  Initialize step are refused, and there is no `/mcp` endpoint in production.
- **Closing sign-up** (see above).
- **Other sign-in providers.** GitHub is the only one, and its two-factor authentication is required;
  there is no GitHub-less sign-in for end users and no multi-factor step for the bootstrap password
  account.
- **Recovery of a locked-out admin**, **deleting** an organization or a user, and a list of your
  sessions.

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
  keys have to be entered again.

Back up `secret.key` together with each dump, and treat the pair as sensitive as the live data.

## Security headers and hosts

What the portal does differently in production is on the
[security model](../portal/security.md#production-mode) page.
