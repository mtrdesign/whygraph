# Configuration

`whygraph.toml` at your repo root is WhyGraph's **headless configuration** - what `whygraph scan`
reads in a CI job or a plain checkout. It is also the shape of a project's configuration everywhere
else: the WhyGraph portal stores the same tree per project, and imports an existing `whygraph.toml`
when you add the repo. Every field has a built-in default, so an unedited file behaves exactly as if
none were present.

On a terminal, `whygraph init` walks you through the common choices (agent, analyze/rationale LLMs +
keys, source-control forge + token, and the auto-rescan git hooks) and writes both a fully-commented
`whygraph.example.toml` (secret-free, committable) and a ready-to-run `whygraph.toml` (with any
secrets you entered). You can always edit `whygraph.toml` by hand afterwards, or start from the
example. `whygraph init --yes` skips the prompts and uses defaults.

!!! warning "`whygraph.toml` is gitignored - never commit a token"
    `init` adds `whygraph.toml` to `.gitignore` precisely because it can hold API keys. Keep it that
    way. Use `whygraph.example.toml` (committed) for documentation, `whygraph.toml` (ignored) for
    secrets.

## The full tree

The values shown are the defaults.

```toml
log_level = "INFO"            # DEBUG | INFO | WARN | ERROR | CRITICAL

[scan]
forge = "off"                 # source-control forge for the PR/issue crawl:
                              #   "off"    - skip the remote crawl (default)
                              #   "github" - pull PRs/issues from the GitHub remote
                              #   "auto"   - detect from the remote URL (github only, for now)
remote = "origin"             # git remote whose URL is inspected for forge github/auto
# token = "ghp_..."           # GitHub token for the gh CLI. Default: read GH_TOKEN /
                              # GITHUB_TOKEN from env (or an existing `gh auth login`).
hooks = true                  # auto-rescan git hooks installed by `whygraph init`:
                              #   true    - all four (post-commit, post-merge,
                              #             post-rewrite, post-checkout)
                              #   false   - none (init removes any already installed)
                              #   [list]  - only these, e.g. ["post-commit", "post-merge"]
# default_branch = "main"     # the branch treated as "shipped history". Default:
                              # resolved from origin/HEAD, else origin/main, else
                              # origin/master. Set for repos on develop / trunk.

[llm]
# model = "anthropic/claude-opus-4-7"  # default model for every role, "provider/model"

[analyze]
# provider = "anthropic"      # which [llm.*] adapter writes per-commit descriptions
# model = "claude-haiku-4-5"  # override the model for analysis only
max_workers = 2               # parallel LLM calls in the diff-analyzer crawler
# max_diff_chars = 50000      # diff truncated past this length before prompting
# large_commit_file_count = 30  # commits touching more files are described per-file on demand
# pr_origin_min_commits = 5   # recover a squash-merged PR's original commits past this size

[rationale]
# provider = "anthropic"      # which [llm.*] adapter writes the rationale card
# model = "claude-haiku-4-5"  # override the model for rationale only
# pr_roster_max_commits = 30      # squashed-commit headlines shown per PR in the prompt
# pr_discussion_max_comments = 20 # PR comments shown per PR in the prompt
# pr_comment_max_chars = 500      # each PR comment clipped to this length

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

# `claude_cli` (Python attribute) and `claude-cli` (TOML idiom) both parse.
[llm.claude_cli]
# api_key = "sk-ant-..."        # default: subscription billing (strips the env var)
timeout_sec = 120
# config_dir = "~/.claude-work"  # Claude Code profile dir (sets CLAUDE_CONFIG_DIR);
                                 # default: inherit the ambient one / ~/.claude
```

## Section by section

| Section | What it controls |
|---|---|
| top-level `log_level` | Console log verbosity. |
| `[scan]` | The crawl: which source-control forge to pull PRs and issues from, the git remote name, an optional pinned GitHub token, which [auto-rescan hooks](../guide/scanning.md#keep-it-fresh) to install, and which branch counts as [shipped history](../guide/scanning.md#how-whygraph-sees-branches). |
| `[llm]` | `model` - the default `"provider/model"` for every role. See [Choosing models](#choosing-models). |
| `[analyze]` | The per-commit LLM diff descriptions written during `scan` - provider, model, parallelism (`max_workers`), and the truncation / per-file thresholds. |
| `[rationale]` | The `whygraph_rationale_brief` card - provider, model, and how much of a squash-merged PR is rendered into the prompt. |
| `[chat]` | The [chat assistant](../guide/chat.md) - default provider and model for new sessions, plus the per-turn tool, generation, and context budgets. |
| `whygraph_db` / `codegraph_db` | Override either database path. |
| `[logging]` | An optional rotating file log, in addition to the always-on stderr log. |
| `[llm.*]` | Per-provider **connection** settings - key, timeout, and `base_url` / `host` where relevant. Six adapters; only four can drive chat. See [LLM providers](llm-providers.md). |

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

Chat needs a provider that can stream tool calls. If `[llm].model` names `ollama` or `claude-cli`,
chat falls back to `anthropic`; naming one of those in `[chat]` itself is an error.

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

Omit an `api_key` from an `[llm.*]` table and WhyGraph reads the standard environment variable
instead.

| Variable | Used for |
|---|---|
| `ANTHROPIC_API_KEY` | The `anthropic` LLM adapter. |
| `OPENAI_API_KEY` | The `openai` LLM adapter. |
| `DEEPSEEK_API_KEY` | The `deepseek` LLM adapter. |
| `OPENROUTER_API_KEY` | The `openrouter` LLM adapter. |
| `GH_TOKEN` / `GITHUB_TOKEN` | The `gh` CLI during the remote crawl, when `[scan].token` is unset. |

These three are read by the Docker shims rather than by WhyGraph itself:

| Variable | Used for |
|---|---|
| `WHYGRAPH_IMAGE` | Override the image a single shim invocation runs. |
| `WHYGRAPH_PORT` | The port for [`whygraph serve`](../guide/playground.md) (default `8765`). |
| `WHYGRAPH_VERSION` | Pin the version at install time. See [Installation](../getting-started/installation.md). |

!!! tip "Provider keys degrade gracefully - for scan and rationale"
    Missing a key for the analysis or rationale phase isn't fatal - that phase skips, and the rest of
    the scan still runs. Descriptions and cards backfill once a credential is available.

    **Chat is the exception.** There's nothing useful to return without a model, so a turn with no
    usable credential fails immediately with an error naming the variable it needs.
