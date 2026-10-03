# Run in production

!!! note "Unreleased"
    Production mode is on `main` and is not in any released image yet; docs deploy from `main`. The
    compose file below pins the current release's image, which does not have it. Until a release ships
    it, build the image from the repository (`make image`) and point the compose file's portal image
    at it. This page describes the change on
    its way and may move until then.

Production mode runs the portal for **more than one person**: people register with an email and a
password, sessions live on the server, and every organization gets its own address,
`<org>.<your host>`. It is the same image and the same portal as [local mode](docker.md), selected by
`WHYGRAPH_MODE=production` at the first start; the mode can't be changed afterwards.

This release gets you identity and organizations. Production organizations hold **no projects yet** -
see [what isn't there yet](#what-isnt-there-yet).

## What you need

- A host with Docker and Docker Compose, reachable on ports 80 and 443.
- A **dedicated registrable domain** (or a domain you give to WhyGraph alone) for the base host.
- DNS you can edit, and an API token for the DNS host if you use the bundled Caddy build (Cloudflare
  in the example).

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
docker compose up -d --build
```

`.env` holds three values: `WHYGRAPH_BASE_URL` (for example `https://whygraph.example.com`, scheme and
host, no path), `WHYGRAPH_HOST` (the same host without the scheme) and `CLOUDFLARE_API_TOKEN`.
`.env` and `secrets/` are gitignored. The database password lives only in the secret file, which is
handed to both containers as a file and never as an environment value.

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
one does. Until it is used, register and sign-in answer that the instance is not claimed yet, so
nobody can take the admin's email or grab org names first.

## Open registration

!!! warning "Anyone who can reach the base host can create an account and organizations"
    Registration is always open in this release, and **it cannot be closed yet**. Each organization
    reserves a subdomain, and there is no cap and no way to disable an abusive account (an admin can
    only reset its password). Sign-up bursts are throttled, nothing more.

    Until a later release adds both, **restrict network access to the base host** - a VPN, or an IP
    allowlist in Caddy - unless you want strangers on it.

Email addresses are only login names: no email is sent or verified, so there is no verification
step and no email reset.

## Passwords

A password must be **15 to 256 characters**, must not be on a list of common passwords, and must not
be the account's email or its local part. There are no composition rules; a passphrase works well.
Passwords are hashed with argon2, and repeated failed sign-ins are throttled per account and address.

## Instance admins

An instance admin manages the instance from `/admin` on the base host: users, who is an admin, the
list of organizations and the [self-check](#the-self-check).

- **Keep two admins.** The last admin can't be demoted, and there is no recovery path for a locked-out
  admin in this release.
- **Reset links.** There is no email, so an admin issues a one-time **reset link** for a user (valid
  24 hours) and hands it over out of band. The token is in the URL fragment, so it never reaches a
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
bootstrap claimed, registration, sign-in (success and failure), sign-out, password change, reset link
issued and used, admin granted or revoked, organization created, and instance-admin read request.
Each carries the event, the user, the target user, the client address and the host. A sign-in failure
shows only the first 3 characters of the email and its domain. No token, secret or password is ever
logged.

They go to the portal's normal log: `docker compose logs portal`. A persistent audit table is not part
of this release, so ship the log somewhere if you need history.

## What isn't there yet

- **Email**: no verification, no reset mail, no invitations.
- **Projects**: production organizations hold none. Adding a project, shared folders and the
  Initialize step are refused, and there is no `/mcp` endpoint in production.
- **Disabling a user** and **closing registration** (see above).
- **Multi-factor and OAuth sign-in**, **recovery of a locked-out admin**, **deleting** an organization or
  a user, and a list of your sessions.

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
