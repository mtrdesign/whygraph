# Security model

In local mode the portal is built for **one person on their own machine**. It has no login. What keeps
it safe is where it listens, what it refuses to do, and what it is never given. This page lists those
boundaries so you can judge them.

## It listens on loopback only

`whygraph up` publishes the portal on `127.0.0.1` and nothing else, and runs it on its own Docker
network so other containers on the default bridge cannot reach its listener either. Outside the
image, `whygraph portal` refuses any non-loopback `--host` unless you pass `--dev-expose`, which
exists for development.

## The database is network-only

The portal's own database runs in a second container, `whygraph-portal-postgres`, that **publishes no
port**: it is reachable only from containers on the portal's `whygraph-portal` network, never from your
host or your LAN.

- Its password is generated on the first `whygraph up` into `postgres.password` in the data directory,
  mode `0600`. It is mounted into both containers as a file, so it is never in an environment value, a
  command line, `docker inspect`, a log line or the portal's API.
- Every data directory gets its own password, so another WhyGraph portal on the same network (a
  development one, say) cannot log in to yours.
- Inside the database container, local connections need no password - that is how `whygraph backup`
  runs `pg_dump`. Anyone who can `docker exec` into it is already your user, who owns the database's
  files in the data directory anyway: the boundary is the same.

## Your browser cannot be used against it

A local server with no login is a target for any web page you visit. The portal closes the usual
routes:

- Every `/api` request, `GET`s included, must carry an `X-WhyGraph-Client: 1` header. A web page
  cannot add a custom header to a cross-origin request without a permission check the portal never
  grants, and the portal never sends CORS headers.
- `/api` and `/mcp` reject `Sec-Fetch-Site: cross-site` and `same-site`, and any `Origin` that is not
  exactly the portal's own address (`null` included).
- The `Host` header must be `127.0.0.1`, `localhost` or `[::1]` with the portal's port, which defeats
  DNS rebinding: a rebinding domain carries its own name in `Host`.

## Shared machines

!!! warning "Local mode is for single-user machines"
    The portal is reachable by **every user of the machine** through the loopback address, and it
    holds your LLM API keys and GitHub tokens. On a machine that other people log in to, another user
    can open the portal, read your projects' history and spend your keys. Do not run local mode on a
    shared host. For several people, use [production mode](../deploy/production.md), which has real
    accounts; otherwise use one machine per person.

The Welcome screen says the same thing the first time you open the portal.

## Production mode

!!! note "Unreleased"
    Production mode is on `main` and in no released image yet. Setup is in
    [Run in production](../deploy/production.md).

Everything above describes local mode. In production mode the portal is reached over the network, so
the checks change:

- **Sign-in is GitHub.** Users sign in through an OAuth App with the read-only `read:user` scope and
  GitHub two-factor authentication required. The portal sends a random `state` and a PKCE challenge
  (S256) whose verifier it keeps in memory, bound to a host-only cookie, so the callback only works in
  the browser that started it and only once, within 10 minutes. GitHub redirects to a page of the
  app (`/auth/github`), which strips the one-time code from the address bar before posting it, so the
  `/api` origin checks need no exception, and the access log redacts that query. The portal reads the
  user, then **revokes GitHub's token** at once and keeps none. It stores the GitHub id (the identity),
  username, display name and avatar, **never an email**. Only the bootstrap admin has a password
  (argon2, 15 characters minimum, a common-password blocklist). A disabled account is refused at
  sign-in and its sessions end at once.
- **Sessions.** The session is a random token whose hash only is stored in the database; the cookie is
  `HttpOnly`, `SameSite=Lax`, `Secure` over `https`, and set on the base host so it reaches every
  organization. Sessions last 30 days at most and 7 days idle, a password change rotates the current
  one and ends the others, and a reset ends all of them. Sign-in, setup and reset happen on the
  base host only.
- **Hosts.** The `Host` must be the base host or exactly one valid label under it
  (`<org>.<base host>`); anything else, including `www.` and lookalike suffixes, gets `421`. An
  organization is served only on its own host, and membership is checked on every request.
- **Origin.** `X-WhyGraph-Client` is still required, `cross-site` and `same-site` requests to `/api`
  are refused, and an `Origin` must be exactly the request's own host, so a page on one
  organization cannot write to another's host. No CORS headers are sent.
- **Headers.** Every response carries `X-Frame-Options: DENY`, a `frame-ancestors 'none'` content
  security policy, `Referrer-Policy: same-origin` and `nosniff`; `/api` responses and any response
  that sets a cookie are `no-store`; over `https` there is HSTS with `includeSubDomains`. A full
  script content security policy is not part of this release.
- **Throttling.** Password sign-in failures are limited per account and address, GitHub sign-in starts
  and callbacks, setup and reset per address, adding members per organization (60 an hour, so the
  add form cannot be used to probe which usernames have accounts), and imports per organization (30
  an hour). Webhook deliveries are not throttled: the signature check rejects a forged one first.
- **Roles.** Members read and use projects; admins also manage people and per-project settings and
  keys; only owners touch owners and change the organization's settings and organization-level keys.
  Chat sessions are private to the user who started them. See
  [Members](../deploy/production.md#members).
- **No user-controlled markup** is rendered on an organization host, because the session cookie is
  shared across them.
- **A dedicated base host.** The cookie reaches every subdomain, so the base host must belong to
  WhyGraph alone. See [why](../deploy/production.md#why-a-dedicated-domain).
- **Instance admins** can read every organization, read-only, and every such request is logged.
  Authentication events go to the `whygraph.portal.audit` log.
- **Projects come only from GitHub, through the GitHub App.** The server refuses any other source.
  The app's private key never leaves the portal process: for each fetch and scan the portal mints
  an **installation token scoped to the one repository** and to read-only access (contents,
  metadata, pull requests, issues), valid for at most an hour. A scan receives it through a file in
  the data directory (`runs/<id>.token`, mode `0600`, written atomically), never its environment;
  the portal rewrites the file before the token expires and deletes it when the scan ends, and
  leftovers are removed at start. CodeGraph never sees that file's name.
- **No personal access tokens.** Storing a GitHub token is refused in production; every GitHub call
  a project makes uses the app's token. The import page's user authorization (8 hours) is kept in
  memory for the session, never in the database or a response, and must belong to the GitHub
  account the user signed in with.
- **Webhooks are verified first.** `POST /github/webhook` is served only on the base host. The body
  is capped at 5 MiB, `X-Hub-Signature-256` is required and compared in constant time against the
  raw body **before** anything is parsed (the older SHA-1 header alone is refused), and a missing or
  wrong signature is answered `401` and logged as `webhook_rejected`. A replayed delivery id is
  ignored. Projects are found by GitHub's numeric repository and installation ids, never by name.
- **A repository that tracks WhyGraph's state is refused.** A repository whose commits contain
  `.whygraph/` or `.codegraph/` is not imported, and a fetch that finds them on the default branch
  fails before the checkout: a committed database would otherwise be served to every member. See
  [Run in production](../deploy/production.md#repositories-that-track-whygraphs-state).
- **Git ignores global and system config.** Every git process for a server copy runs with
  `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_NOSYSTEM=1`, so an `insteadOf`, `http.*` or `include`
  in the host's git config cannot redirect a fetch. `origin` is set from the repository id before
  every fetch, and the checkout runs with hooks disabled.
- **What is off.** No shared folders, no local repositories, no `/mcp`, no git-hook scans. Agents
  reach a production portal's projects through a [connected portal](#connected-portals), not directly.

## Connected portals

A [platform project](platform-projects.md) lets a local portal read a platform's history for one
project. The credential for that is a **connection token**, and the design assumes the local portal
runs on a developer's laptop and a repository on it is untrusted.

- **One token reaches one project, as one person.** The token is `wgc_` plus 32 random bytes. The
  platform stores only its SHA-256, so a database read cannot be replayed as a token. It names a
  project by id, never by slug, and the person's role and membership are re-checked on **every call**,
  so a role change applies to the next request and a removed member is cut off at once. An instance
  admin's read-only access never gets a token, and a token never carries an administrator's powers or
  a session.
- **Revocation follows membership.** Removing a member, a member leaving, disabling an account,
  deleting a project or an organization, an admin revoking one token, and "Remove from this machine"
  all revoke the affected tokens, each with a reason the local portal can show. **Re-enabling a
  disabled account does not restore its tokens**: link again. A token unused for 90 days expires, and
  one never used expires after an hour, which cleans up a link abandoned halfway.
- **Linking is OAuth 2.0 authorization code with PKCE (S256).** The local portal is a native app
  on a port the platform cannot know in advance, so the redirect is exactly
  `http://127.0.0.1:<port>/connect/callback`. The code lasts 60 seconds, works once, and is spent even
  when its verification fails. The platform builds the redirect itself, from a request it validated
  before rendering anything, so the consent page can only send the browser to a URL the server
  returned.
- **The `iss` check.** Every redirect carries the platform's own address (RFC 9207), and the local
  portal refuses a callback whose `iss` is not the platform it started with, **before** it uses the
  code. PKCE alone does not stop a rogue platform from being swapped in mid-flow; this does.
- **`/api/v1` takes a bearer token and nothing else.** On that path the platform reads the
  `Authorization: Bearer` header and **never a cookie**, and everywhere else it ignores a bearer
  header and reads only the session cookie. A bearer token is not ambient, so a web page cannot use
  one, and the CSRF checks of the session routes need nothing new. Failed token lookups are throttled
  per address; evidence and rationale requests are throttled per token, limited to two at a time
  per organization, and capped in `git` work and body size per request. Git's own error text never
  appears in a response.
- **The token is never exposed.** It lives encrypted in the local portal's database (the same Fernet
  store as other secrets), is decrypted only into the client that calls the platform, and is never in
  an API response, a log line, an error message, a command line or a child process's environment.
  `wgc_` tokens are masked like GitHub's in anything a run writes. A scan child gets no token: the child
  environment is an allowlist the token is not on.
- **Only pushed data leaves your machine.** See [what leaves your
  machine](platform-projects.md#what-leaves-your-machine). The client also checks every request body
  against the same models the platform enforces, and caps what it reads back.
- **A platform address is `https`.** The local portal calls exactly two origins, the platform's base
  host and its organization host, follows no redirects, and refuses `http` (a development switch
  allows loopback only).
- **The local pages cannot be framed.** In local mode every page the portal serves, not only
  `/api`, carries `X-Frame-Options: DENY` and `frame-ancestors 'none'`, so `/link` and
  `/connect/callback` cannot be clickjacked. Local mode still sets no CORS headers.
- **The deep link carries no secret.** **Use with your agent** links to
  `http://127.0.0.1:<port>/link` with the platform, organization and project names in the query. It
  grants nothing: the local portal still runs the consent flow, and the platform page never contacts
  the local one.
- **Visible to admins.** An owner or admin of a project sees the machine name each member gave
  their connection, and can revoke it. See [Connected
  portals](../deploy/production.md#connected-portals).

## Keys and tokens

- LLM API keys and GitHub tokens are stored in the portal database **encrypted** (Fernet), and the API
  is write-only for them: the UI shows `set ...a1b2`, never the key.
- The encryption key is `secret.key` in the data directory. This protects a **database dump** (a
  `whygraph backup` file, say) or a copy of the database's files on their own. It does not protect
  against someone who can read your data directory, which holds both, which is why that directory is
  `0700` and the portal will not share a folder that contains it. A dump together with `secret.key` is
  as sensitive as the live data directory; `backups/` is `0700` too.
- **Nothing from your shell environment enters the container.** Keys you export in your shell are not
  passed to the portal.
- A scan runs as a child process with an allowlisted environment (`PATH`, `HOME`, locale, `TZ`, TLS and
  proxy variables). Keys and tokens reach it only as the variables that one scan needs, and any key
  that appears in a run's progress or log file is masked to its last four characters.
- A GitHub token used for a fetch (in production, the installation token) is handed to git through a
  host-scoped credential helper. It is not in `.git/config`, argv, logs or run files, and is not sent
  to any other host. Anything shaped like a GitHub token in a run's output is masked, even when a
  tool already masked part of it.
- Changing a provider's endpoint clears the key stored for the old one, and an endpoint found in a
  repository's `whygraph.toml` is never imported. A committed file cannot redirect your key to another
  server.

## Repository content is not trusted

A repository - a GitHub clone above all - can contain anything. The portal treats it that way.

### Symbolic links

The portal never follows a symbolic link out of a project's folder. When `.whygraph/`, `.codegraph/`,
either database, `.gitignore`, `whygraph.toml`, a portal marker, an agent config file or a bundled
agent folder (such as `.claude/`) is a link, the portal refuses it with an `unsafe_path` error:
initializing, the Explorer, Chat, the MCP endpoint and scans all stop for that project, a
`whygraph.toml` link is not imported, and removing the project leaves the linked files alone. Replace
the link with a real file or folder to continue.

### Configuration from a repository

An imported `whygraph.toml` contributes only an allowlist of settings (models, tuning keys and a few
`[scan]` keys). Database paths are always `<repo>/.whygraph/whygraph.db` and
`<repo>/.codegraph/codegraph.db`, whatever a file says. A `remote` or `default_branch` value that could be
read by git as an option is dropped.

### Hooks

The git hook helper lives in the git directory (`.git/whygraph/whygraph-scan`), not in the working
tree, so a commit pulled from a remote cannot replace it: git never checks files out into `.git/`. It
reads `.whygraph/portal.env` with `sed` and validates the two values it takes (a slug and a port); it
never `source`s the file. It ignores the file if git tracks it or it is a symbolic link, and it never
writes its log or pending flag through a symbolic link. It only ever sends a request to `127.0.0.1`
and never runs a scan itself.

### Clones

Local mode clones nothing: a local project is a folder you shared. In production the portal clones
only a repository the GitHub App covers, from `WHYGRAPH_GITHUB_URL` (`https` except on loopback), with
a restricted protocol list, into `repos/<org>/<project>` under its data directory. It removes a failed
clone it created and will not delete a directory it did not.

## What the portal writes to your repository

Only when you initialize a project, only what the preview listed, and only after you confirm:
`.gitignore` entries, git hooks, agent MCP entries and assets, and the two marker files in
`.whygraph/`. Agent files you did not name are not touched, unparseable files are refused rather than
overwritten, tracked files need your confirmation, and a backup is kept under `.whygraph/backups/`.

Adding a project without initializing it writes nothing.
