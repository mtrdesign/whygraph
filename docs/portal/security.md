# Security model

The portal is built for **one person on their own machine** (local mode). It has no login. What keeps
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
    shared host. A multi-user mode with real authentication is planned; until then, use one machine
    per person.

The Welcome screen says the same thing the first time you open the portal.

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
- A GitHub token used for a clone is handed to git through a host-scoped credential helper. It is not
  in `.git/config`, argv, logs or run files, and is not sent to any other host.
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

Only `https://github.com/<owner>/<repo>` URLs are cloned, with a restricted protocol list. The portal
removes a failed clone it created and will not delete a directory it did not.

## What the portal writes to your repository

Only when you initialize a project, only what the preview listed, and only after you confirm:
`.gitignore` entries, git hooks, agent MCP entries and assets, and the two marker files in
`.whygraph/`. Agent files you did not name are not touched, unparseable files are refused rather than
overwritten, tracked files need your confirmation, and a backup is kept under `.whygraph/backups/`.

Adding a project without initializing it writes nothing.
