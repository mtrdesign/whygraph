# Configuration

WhyGraph reads an optional `whygraph.toml` at your repo root. Every field has a built-in default, so
an unedited file behaves exactly as if none were present. `whygraph init` scaffolds a fully-commented
`whygraph.example.toml` for you - copy it to `whygraph.toml` and edit what you need.

!!! warning "`whygraph.toml` is gitignored - never commit a token"
    `init` adds `whygraph.toml` to `.gitignore` precisely because it can hold API keys. Keep it that
    way. Use `whygraph.example.toml` (committed) for documentation, `whygraph.toml` (ignored) for
    secrets.

## The full tree

The values shown are the defaults.

```toml
log_level = "INFO"            # DEBUG | INFO | WARN | ERROR | CRITICAL

[scan]
max_workers = 2               # parallel LLM calls in the diff-analyzer crawler
provider = "off"              # source-control backend for the PR/issue crawl:
                              #   "off"    - skip the remote crawl (default)
                              #   "github" - pull PRs/issues from the GitHub remote
                              #   "auto"   - detect from the remote URL (github only, for now)
remote = "origin"             # git remote whose URL is inspected for provider="auto"
# token = "ghp_..."           # GitHub token for the gh CLI. Default: read GH_TOKEN /
                              # GITHUB_TOKEN from env (or an existing `gh auth login`).

[analyze]
provider = "anthropic"        # which [llm.*] adapter writes per-commit descriptions
# model = "claude-haiku-4-5"  # override the provider's model for analysis only
# max_diff_chars = 50000      # diff truncated past this length before prompting
# large_commit_file_count = 30  # commits touching more files are described per-file on demand
# pr_origin_min_commits = 5   # recover a squash-merged PR's original commits past this size
# timeout_sec = 60            # per-call timeout; default: the adapter's own

[rationale]
provider = "anthropic"        # which [llm.*] adapter writes the rationale card
# model = "claude-haiku-4-5"  # override the provider's model for rationale only
# timeout_sec = 60            # per-call timeout; default: the adapter's own
# pr_roster_max_commits = 30      # squashed-commit headlines shown per PR in the prompt
# pr_discussion_max_comments = 20 # PR comments shown per PR in the prompt
# pr_comment_max_chars = 500      # each PR comment clipped to this length

# Override default DB locations (resolved relative to this file):
# whygraph_db  = ".whygraph/whygraph.db"
# codegraph_db = ".codegraph/codegraph.db"

# Optional rotating file log. Console (stderr) logging is always on.
# [logging]
# file         = ".whygraph/logs/whygraph.log"
# level        = "DEBUG"        # default: inherit top-level log_level
# max_bytes    = 5_000_000
# backup_count = 3

[llm.anthropic]
model = "claude-opus-4-7"
# api_key = "sk-ant-..."        # default: read ANTHROPIC_API_KEY from env
timeout_sec = 60

[llm.openai]
model = "gpt-4o"
# api_key = "sk-..."            # default: read OPENAI_API_KEY from env
# base_url = "..."              # default: https://api.openai.com/v1
timeout_sec = 60

[llm.deepseek]
model = "deepseek-chat"
# api_key = "sk-..."            # default: read DEEPSEEK_API_KEY from env
timeout_sec = 60

[llm.ollama]
model = "llama3"
# host = "http://localhost:11434"
timeout_sec = 120

# `claude_cli` (Python attribute) and `claude-cli` (TOML idiom) both parse.
[llm.claude_cli]
model = "claude-opus-4-7"
# api_key = "sk-ant-..."        # default: subscription billing (strips the env var)
timeout_sec = 120
```

## Section by section

| Section | What it controls |
|---|---|
| top-level `log_level` | Console log verbosity. |
| `[scan]` | The crawl: parallelism, which remote provider to use, the git remote name, and an optional pinned GitHub token. |
| `[analyze]` | The per-commit LLM diff descriptions written during `scan` - provider, model, and the truncation / per-file thresholds. |
| `[rationale]` | The `whygraph_rationale_brief` card - provider, model, and how much of a squash-merged PR is rendered into the prompt. |
| `whygraph_db` / `codegraph_db` | Override either database path. |
| `[logging]` | An optional rotating file log, in addition to the always-on stderr log. |
| `[llm.*]` | Per-provider client settings - model, key, timeout, and `base_url` / `host` where relevant. |

## Environment variables

Omit an `api_key` from an `[llm.*]` table and WhyGraph reads the standard environment variable
instead.

| Variable | Used for |
|---|---|
| `ANTHROPIC_API_KEY` | The `anthropic` LLM adapter. |
| `OPENAI_API_KEY` | The `openai` LLM adapter. |
| `DEEPSEEK_API_KEY` | The `deepseek` LLM adapter. |
| `GH_TOKEN` / `GITHUB_TOKEN` | The `gh` CLI during the remote crawl, when `[scan].token` is unset. |

!!! tip "Provider keys degrade gracefully"
    Missing a key for the analysis or rationale phase isn't fatal - that phase skips, and the rest of
    the scan still runs. Descriptions and cards backfill once a credential is available.
