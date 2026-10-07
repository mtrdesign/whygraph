# Configuration

WhyGraph has one configuration **shape**, used in two places:

- In the **[portal](../portal/index.md)** - the normal way - configuration is stored per project in the
  portal's database and edited in the UI: the **Settings** page holds defaults for every project, and a
  project's own settings override them. API keys and GitHub tokens are stored encrypted there.
- **`whygraph.toml`** at a repo root is the **headless** configuration: what `whygraph scan` reads in a CI
  job or a plain checkout, and what the portal offers to import when you add the repo. Every field has a
  built-in default, so an unedited file behaves exactly as if none were present.

Nothing writes `whygraph.toml` for you any more (`whygraph init` was removed). Create one by hand when
you need headless configuration, from the tree below.

!!! warning "`whygraph.toml` is gitignored - never commit a token"
    A `whygraph.toml` can hold API keys, so keep it out of git. The portal adds it to `.gitignore` when it
    initializes a project, and moves any keys in it into its encrypted store when it imports the file.

## In the portal

For each project the portal builds its configuration from two layers, the **project** layer winning:

1. the global defaults from **Settings** (models, provider endpoints; provider keys are inherited too);
2. the project's own settings.

Two things differ from a `whygraph.toml`:

- **Keys and tokens are entered in the UI, not read from your environment.** The portal and its scans
  are started with none of your shell's variables - `ANTHROPIC_API_KEY`, `GH_TOKEN` and the rest do not
  reach them. A project's own key for a provider wins over the global one, and a global key is never
  sent to a project that overrides that provider's endpoint.
- **Only some keys can be set.** Database paths are always `<repo>/.whygraph/whygraph.db` and
  `<repo>/.codegraph/codegraph.db`. Importing a repository's `whygraph.toml` takes models, the
  `[analyze]` / `[rationale]` / `[chat]` tuning keys, and `[scan].forge`, `remote`, `default_branch` and
  `hooks`. It never takes endpoints, database paths or the log file, drops any setting for the removed
  [`claude-cli` provider](llm-providers.md#claude-cli-removed), and reports what it dropped. Endpoints
  are set in the UI.

To reach an Ollama daemon or a gateway on your machine from the portal's container, use
`host.docker.internal` - see [Adding projects](../portal/projects.md#ollama-and-gateways-on-your-machine).

## The full tree

The values shown are the defaults. This is the tree a `whygraph.toml` uses; the portal stores the same
tree per layer.

```toml
log_level = "INFO"            # DEBUG | INFO | WARN | ERROR | CRITICAL

[scan]
forge = "off"                 # source-control forge for the PR/issue crawl:
                              #   "off"    - skip the remote crawl (default)
                              #   "github" - pull PRs/issues from the GitHub remote
                              #   "auto"   - detect from the remote URL (github only, for now)
remote = "origin"             # git remote whose URL is inspected for forge github/auto
                              # (a plain name: letters, digits, ".", "_", "/", "-")
# token = "ghp_..."           # GitHub token for the gh CLI. Default: read GH_TOKEN /
                              # GITHUB_TOKEN from env (or an existing `gh auth login`).
hooks = true                  # auto-rescan git hooks installed by the portal:
                              #   true    - all four (post-commit, post-merge,
                              #             post-rewrite, post-checkout)
                              #   false   - none (the portal removes any already installed)
                              #   [list]  - only these, e.g. ["post-commit", "post-merge"]
# default_branch = "main"     # the branch treated as "shipped history". Default:
                              # resolved from origin/HEAD, else origin/main, else
                              # origin/master. Set for repos on develop / trunk.
                              # Must not start with "-".

[llm]
# model = "anthropic/claude-opus-4-7"  # default model for every role, "provider/model"

[analyze]
# provider = "anthropic"      # which [llm.*] adapter writes per-commit descriptions
# model = "claude-haiku-4-5"  # override the model for analysis only
max_workers = 2               # parallel LLM calls in the diff-analyzer crawler
# max_diff_chars = 50000      # diff truncated past this length before prompting
# large_commit_file_count = 30  # commits touching more files are described per-file on demand
# pr_origin_min_commits = 5   # recover a squash-merged PR's original commits past this size
# agent_descriptions_per_hour = 600  # org setting only, see "Agent limits" below

[rationale]
# provider = "anthropic"      # which [llm.*] adapter writes the rationale card
# model = "claude-haiku-4-5"  # override the model for rationale only
# pr_roster_max_commits = 30      # squashed-commit headlines shown per PR in the prompt
# pr_discussion_max_comments = 20 # PR comments shown per PR in the prompt
# pr_comment_max_chars = 500      # each PR comment clipped to this length
# agent_generations_per_hour = 120  # org setting only, see "Agent limits" below

[chat]
# The chat assistant. Provider/model are DEFAULTS for new sessions only -
# each session stores the pair it was started with.
# provider = "anthropic"      # anthropic | openai | deepseek | openrouter
# model = "claude-opus-4-7"   # default: [llm].model, then the provider's default
# max_tool_rounds = 8         # hard bound on tool rounds per user turn
# max_rationale_generations = 2  # uncached rationale cards one turn may generate
                              # (0 = cache-only). Bounds nested LLM spend.
# context_token_budget = 60000   # history sent to the model, ~chars/4 estimate

# Override default DB locations (resolved relative to this file):
# whygraph_db  = ".whygraph/whygraph.db"
# codegraph_db = ".codegraph/codegraph.db"

# Optional rotating file log. Console (stderr) logging is always on.
# [logging]
# file         = ".whygraph/logs/whygraph.log"
# level        = "DEBUG"        # default: inherit top-level log_level
# max_bytes    = 5_000_000
# backup_count = 3

# Per-provider connection settings - key, endpoint, timeout. No model here:
# pick models with [llm].model or a role's own model.
[llm.anthropic]
# api_key = "sk-ant-..."        # default: read ANTHROPIC_API_KEY from env
timeout_sec = 60

[llm.openai]
# api_key = "sk-..."            # default: read OPENAI_API_KEY from env
# base_url = "..."              # default: https://api.openai.com/v1
timeout_sec = 60

[llm.deepseek]
# api_key = "sk-..."            # default: read DEEPSEEK_API_KEY from env
timeout_sec = 60

[llm.openrouter]
# api_key = "sk-or-..."         # default: read OPENROUTER_API_KEY from env
timeout_sec = 60

[llm.ollama]
# host = "http://localhost:11434"
timeout_sec = 120
```

## Section by section

| Section | What it controls |
|---|---|
| top-level `log_level` | Console log verbosity. |
| `[scan]` | The crawl: which source-control forge to pull PRs and issues from, the git remote name, an optional pinned GitHub token (headless only), which [auto-rescan hooks](../guide/scanning.md#keep-it-fresh) to install, and which branch counts as [shipped history](../guide/scanning.md#how-whygraph-sees-branches). |
| `[llm]` | `model` - the default `"provider/model"` for every role. See [Choosing models](#choosing-models). |
| `[analyze]` | The per-commit LLM diff descriptions written during `scan` - provider, model, parallelism (`max_workers`), the truncation / per-file thresholds, and an [org-only agent limit](#agent-limits-organization-only). |
| `[rationale]` | The `whygraph_rationale_brief` card - provider, model, how much of a squash-merged PR is rendered into the prompt, and an [org-only agent limit](#agent-limits-organization-only). |
| `[chat]` | The [chat assistant](../guide/chat.md) - default provider and model for new sessions, plus the per-turn tool, generation, and context budgets. |
| `whygraph_db` / `codegraph_db` | Override either database path. |
| `[logging]` | An optional rotating file log, in addition to the always-on stderr log. |
| `[llm.*]` | Per-provider **connection** settings - key, timeout, and `base_url` / `host` where relevant. Five adapters; only four can drive chat. See [LLM providers](llm-providers.md). |

## Agent limits (organization only)

Two keys bound the LLM spend that agents on [connected portals](../portal/platform-projects.md) can
cause on a [production](../deploy/production.md) portal. They are different from every other key:

| Key | Default | Range | Bounds |
|---|---|---|---|
| `[rationale].agent_generations_per_hour` | `120` | `0` to `10000` | Uncached rationale cards generated for agents, per hour, across the organization |
| `[analyze].agent_descriptions_per_hour` | `600` | `0` to `10000` | Commits described by the lazy backfill for agents' evidence, per hour, across the organization |

`0` gives agents only what exists: cached cards, and descriptions already written. Cached cards are
always served.

- **Organization defaults only.** They are set by an **owner**, in the organization's **Settings**
  under **Agent limits** (or `PUT /api/portal/defaults`). An admin cannot set them.
- **Never per project.** A project's settings refuse them, and the platform reads them from the
  organization's defaults row, never from a project's merged configuration.
- **Never imported from a repository.** A `whygraph.toml` that sets them has them dropped and
  reported on import, because a repository is untrusted input and must not raise its own organization's
  spend.
- **Production only.** Local mode has no connected portals, so they have no effect there.

## Choosing models

Set one default for everything with `[llm].model`, then override a single role where it pays off:

```toml
[llm]
model = "anthropic/claude-opus-4-7"   # every role, unless it says otherwise

[analyze]
model = "claude-haiku-4-5"            # a cheaper model for per-commit descriptions
```

`[llm].model` is split on the **first** `/`: the part before it is the provider, the rest is the
model. So `"openrouter/openrouter/auto"` means OpenRouter's `openrouter/auto` model.

For each role WhyGraph resolves one `(provider, model)` pair, highest precedence first:

| # | Source | Note |
|---|---|---|
| 1 | The role's own `model` (`[analyze]`, `[rationale]`, `[chat]`) | Written as `"provider/model"` it switches provider too - but only when the table has no `provider` key and the prefix is a known provider, so `provider = "openrouter"` + `model = "openai/gpt-4o"` stays one OpenRouter model. |
| 2 | `[llm].model` | Used when its provider is the role's provider. |
| 3 | `[llm.<provider>].model` | Deprecated - see below. |
| 4 | The provider's built-in default | `claude-opus-4-7`, `gpt-4o`, `deepseek-chat`, `openrouter/auto`, `llama3`. |

The provider comes from the role's `provider`, else the prefix of its `model`, else the provider of
`[llm].model`, else `anthropic`.

Chat needs a provider that can stream tool calls. If `[llm].model` names `ollama`, chat falls back
to `anthropic`; naming it in `[chat]` itself is an error.

!!! note "Rationale cards follow the model"
    Cached rationale cards are keyed on the provider and the *pinned* model. Changing `[llm].model` or
    `[rationale].model` therefore regenerates cards rather than serving ones written by another
    model. With nothing pinned the key is the same one 1.x used, so an upgraded project keeps its
    cache.

## Deprecated keys

These 1.x keys still work through 2.x - each logs one deprecation warning per process - and are
removed in 3.0. When a table sets both the old and the new spelling, the new one wins.

| 1.x key | Use instead |
|---|---|
| `[scan].provider` | `[scan].forge` |
| `[scan].max_workers` | `[analyze].max_workers` |
| `[llm.<provider>].model` | `[llm].model = "<provider>/<model>"`, or a role's own `model` |
| `[analyze].timeout_sec`, `[rationale].timeout_sec` | `[llm.<provider>].timeout_sec` - timeouts belong to the provider |

A 1.x file resolves to exactly the same providers, models, and timeouts it did before.

!!! note "An unknown key is a warning, not an error"
    A typo'd or stale key is logged and ignored rather than aborting the command, so an old config
    keeps working across upgrades. Check the log if a setting doesn't seem to take effect.

## Environment variables

The variables in the first two tables apply to **headless** `whygraph scan`. The portal does not read
them: its container is started with none of your environment, so credentials are entered under
Settings instead. The portal has variables of its own, in the tables further down.

Omit an `api_key` from an `[llm.*]` table and headless WhyGraph reads the standard environment variable
instead.

| Variable | Used for |
|---|---|
| `ANTHROPIC_API_KEY` | The `anthropic` LLM adapter. |
| `OPENAI_API_KEY` | The `openai` LLM adapter. |
| `DEEPSEEK_API_KEY` | The `deepseek` LLM adapter. |
| `OPENROUTER_API_KEY` | The `openrouter` LLM adapter. |
| `GH_TOKEN` / `GITHUB_TOKEN` | The `gh` CLI during the remote crawl, when `[scan].token` is unset. |

One variable replaces the file entirely, for tools that launch `whygraph scan` as a child process:

| Variable | Used for |
|---|---|
| `WHYGRAPH_CONFIG_JSON` | A whole config tree, as a JSON object shaped like `whygraph.toml`. When set and non-empty it is read **instead of** the repo's `whygraph.toml` (which is then ignored, even if present), and relative paths resolve against the repo root. Invalid JSON or a non-object aborts with a config error. The portal uses it to hand a scan its config without writing a file into the checkout. |

These are read by the Docker shim and the portal rather than by a scan:

| Variable | Used for |
|---|---|
| `WHYGRAPH_IMAGE` | Override the image a single shim invocation runs, including `whygraph up`. |
| `WHYGRAPH_PORT` | The portal's port (default `8765`) when `whygraph up --port` has not set one. It is also what Claude Code's agent entry reads, `${WHYGRAPH_PORT:-8765}` - see [Connecting agents](../portal/agents.md#a-non-default-port). |
| `WHYGRAPH_DATA` | The portal's data directory (default `~/.local/share/whygraph`). |
| `WHYGRAPH_VERSION` | Pin the version at install time. See [Installation](../getting-started/installation.md). |
| `WHYGRAPH_POSTGRES_IMAGE` | Shim only. Override the pinned `postgres` image of the database container, for a registry mirror. It must be the same Postgres major version as the pin; a different image recreates the database container (after a dump). |
| `WHYGRAPH_SKIP_BACKUP` | Shim only. Set to `1` to let `whygraph up` recreate a running database container without dumping it first, when that dump fails and you accept the risk. See [Backup and restore](../portal/backup.md). |

The portal reads its database from two variables. The shim sets both for its own database container,
so you only set them to run `whygraph portal` yourself:

| Variable | Used for |
|---|---|
| `WHYGRAPH_DATABASE_URL` | **Required by `whygraph portal`**: the Postgres database the portal keeps its own data in, such as `postgresql+psycopg://whygraph@whygraph-portal-postgres:5432/whygraph`. `postgresql://` and `postgres://` are accepted too; any other database is refused. Without it, `whygraph portal` exits with status `2`. |
| `WHYGRAPH_DATABASE_PASSWORD_FILE` | Optional. A file whose contents (surrounding whitespace and newlines stripped) are the database password; it wins over a password in the URL. The shim uses `/data/postgres.password`, so the password never appears in `docker inspect`, a process list or the environment of what the portal starts. |

Neither is a `whygraph.toml` key: the portal database is host configuration, not project
configuration.

These select and shape the portal's [production mode](../deploy/production.md), and are read once at
its start. None is a `whygraph.toml` key either.

| Variable | Used for |
|---|---|
| `WHYGRAPH_MODE` | `local` (the default) or `production`. It is fixed at the portal's first start and stored in its database; a different value later is refused. |
| `WHYGRAPH_BASE_URL` | **Required in production.** The public address, such as `https://whygraph.example.com` or `http://whygraph.localhost:8765`: scheme and host (and a non-default port), no path. Organizations live on `<org>.<host>`. `http` is accepted only for hosts ending in `.localhost`; an IP address or a single-label host is refused. A default port (`:443`, `:80`) is dropped. An invalid value, or `WHYGRAPH_SHARED_FOLDERS` set in production, stops the first start with exit code `2`. |
| `WHYGRAPH_GITHUB_OAUTH_CLIENT_ID` | **Required in production.** The client ID of the GitHub OAuth App that signs people in; its callback URL is `<WHYGRAPH_BASE_URL>/auth/github`. See [GitHub sign-in](../deploy/production.md#github-sign-in). |
| `WHYGRAPH_GITHUB_OAUTH_CLIENT_SECRET_FILE` | **Required in production.** A file holding that app's client secret (trailing whitespace stripped). The secret is never an environment value. A missing variable, or an unreadable or empty file, stops the start with exit code `2`. |
| `WHYGRAPH_GITHUB_APP_SLUG` | **Required in production.** The URL name of the GitHub App that projects are imported through (`github.com/apps/<slug>`). See [The GitHub App](../deploy/production.md#the-github-app). |
| `WHYGRAPH_GITHUB_APP_CLIENT_ID` | **Required in production.** That app's client ID (not its numeric app ID). |
| `WHYGRAPH_GITHUB_APP_CLIENT_SECRET_FILE` | **Required in production.** A file holding the app's client secret. |
| `WHYGRAPH_GITHUB_APP_PRIVATE_KEY_FILE` | **Required in production.** The app's private key, an unencrypted RSA `.pem` file. It never leaves the portal process. |
| `WHYGRAPH_GITHUB_APP_WEBHOOK_SECRET_FILE` | **Required in production.** A file holding the app's webhook secret, at least 32 characters. A missing app variable, an unreadable or empty file, a key that is not RSA or a short webhook secret stops the start with exit code `2`, naming the variable. |
| `WHYGRAPH_GITHUB_URL` | The GitHub web address, default `https://github.com`; set it for GitHub Enterprise Server. `https` only, except `http` for `localhost` and loopback addresses. Git clones and fetches production projects from it too; the pull request and issue crawl works only for `github.com`. |
| `WHYGRAPH_GITHUB_API_URL` | The GitHub API address, default `https://api.github.com`; on Enterprise Server `<WHYGRAPH_GITHUB_URL>/api/v3`. Same scheme rule. |
| `WHYGRAPH_TRUSTED_PROXIES` | Comma-separated IPs or CIDRs of the reverse proxies whose `X-Forwarded-For` the portal believes (it is uvicorn's `forwarded_allow_ips`). Unset trusts none. A bad entry exits with status `2`. |

!!! tip "Provider keys degrade gracefully - for scan and rationale"
    Missing a key for the analysis or rationale phase isn't fatal - that phase skips, and the rest of
    the scan still runs. Descriptions and cards backfill once a credential is available.

    **Chat is the exception.** There's nothing useful to return without a model, so a turn with no
    usable credential fails immediately with an error naming the variable it needs.
