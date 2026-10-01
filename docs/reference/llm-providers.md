# LLM providers

WhyGraph calls an LLM in three places. All three default to `[llm].model`, and each can override it:

| Role | Config | What it does |
|---|---|---|
| Analysis | `[analyze]` | Writes a per-commit description of the diff during `whygraph scan`. |
| Rationale | `[rationale]` | Writes the five-section rationale card, for MCP, the Explorer, and chat. |
| Chat | `[chat]` | Drives the [chat assistant](../guide/chat.md). |

Six adapters ship. All six can fill the analysis and rationale roles; only four can drive chat,
because chat needs streaming tool calls.

In the portal you choose models and enter keys in the UI (Settings for every project, or per project);
the config sections and environment variables below are the headless form of the same settings. See
[Configuration](configuration.md#in-the-portal).

| Provider | Config section | Credential (headless env var) | Analysis / rationale | Chat |
|---|---|---|---|---|
| `anthropic` | `[llm.anthropic]` | `ANTHROPIC_API_KEY` | Yes | Yes |
| `openai` | `[llm.openai]` | `OPENAI_API_KEY` | Yes | Yes |
| `deepseek` | `[llm.deepseek]` | `DEEPSEEK_API_KEY` | Yes | Yes |
| `openrouter` | `[llm.openrouter]` | `OPENROUTER_API_KEY` | Yes | Yes |
| `ollama` | `[llm.ollama]` | none (local) | Yes | No |
| `claude-cli` | `[llm.claude_cli]` | your Claude subscription | Yes | No |

`ollama` is excluded from chat because local models' tool-calling reliability varies too much to
depend on. `claude-cli` disables tools outright.

## How a provider is configured

Two kinds of table, and they do different jobs. `[llm].model` and the **role** tables say *which*
provider and model to use; each `[llm.<provider>]` table says *how* to reach that provider.

```toml
[llm]
model = "anthropic/claude-opus-4-7"   # "provider/model" - the default for every role

[analyze]
model = "claude-haiku-4-5"            # override this role's model only

[llm.anthropic]                       # how to reach that provider - no model here
# api_key = "sk-ant-..."              # default: read ANTHROPIC_API_KEY from env
timeout_sec = 60
```

Leave a role's `model` unset and it uses `[llm].model`, or the provider's built-in default when that
is unset too. Set it to give that one role a cheaper or stronger model than the rest - a common setup
is a fast model for per-commit analysis and a stronger one for rationale. A role can also switch
provider, with `provider = "..."` or a `"provider/model"` value. The full precedence is in
[Configuration](configuration.md#choosing-models).

!!! note "`model` in `[llm.<provider>]` is deprecated"
    1.x set each provider's default model inside its `[llm.<provider>]` table. That still works
    through 2.x, with a deprecation warning, and is removed in 3.0 - move it to `[llm].model` (or a
    role's `model`). The same goes for a role-level `timeout_sec`: timeouts now live only in
    `[llm.<provider>]`.

In the portal, keys are entered under Settings and stored encrypted; they are write-only in the UI and
never written into your repository. For headless `whygraph scan`, omit `api_key` and the adapter reads
the conventional environment variable - the recommended setup there, since keys in the environment
cannot leak into a commit at all. **Variables in your shell do not reach the portal.**

See [Configuration](configuration.md) for every key.

## Notes per provider

### `openrouter`

The model defaults to `openrouter/auto`, which routes your request automatically. That is fine for
analysis and rationale, but **not every routed model supports tool calling** - pin a specific
tool-capable model when using OpenRouter for chat. OpenRouter model ids contain a `/` themselves;
`[llm].model` splits only on the first one:

```toml
[llm]
model = "openrouter/anthropic/claude-sonnet-4"   # provider "openrouter", model "anthropic/claude-sonnet-4"
```

### `claude-cli`

Runs through your local Claude Code CLI and bills against your **subscription** rather than an API
key. To make that work it deliberately strips `ANTHROPIC_API_KEY` from the subprocess environment, so
having that variable set for other providers does not silently switch you to metered API billing.
Setting `api_key` in `[llm.claude_cli]` explicitly puts it back - that is the opt-in to API billing.

!!! warning "The tag and the section name differ"
    The provider tag is hyphenated but the config section is not:

    ```toml
    [llm]
    model = "claude-cli/claude-opus-4-7"   # hyphen

    [llm.claude_cli]                       # underscore
    timeout_sec = 120
    ```

    Both `[llm.claude_cli]` and `[llm.claude-cli]` parse, so either spelling of the section works.
    The provider value is always `"claude-cli"`, in a role's `provider` and in `[llm].model`.

#### Multiple Claude Code profiles

Claude Code picks its profile - and so which login and subscription gets billed - from the
`CLAUDE_CONFIG_DIR` environment variable, falling back to `~/.claude`. If you keep separate
profiles on one machine (say `~/.claude` for personal and `~/.claude-work` for work), pin the one
this project should use:

```toml
[llm.claude_cli]
config_dir = "~/.claude-work"
```

WhyGraph exports it as `CLAUDE_CONFIG_DIR` for every `claude --print` call, overriding whatever the
calling shell has set. `~` and `$VARS` are expanded, and a relative path resolves against the
directory holding `whygraph.toml`. Leave it unset to inherit the ambient `CLAUDE_CONFIG_DIR`.

This matters most for the places that don't run inside your interactive shell, which would otherwise
quietly fall back to `~/.claude`. The path is machine-specific, so set it in a gitignored
`whygraph.toml`, never a committed file. If the directory does not exist the call
fails with a clear error rather than letting the CLI create an empty, logged-out profile.

#### In the portal: a subscription token

The Docker image ships a pinned `claude` CLI, but not your login - and a folder mount cannot bring
it in, because on macOS Claude Code keeps it in the Keychain. Use a long-lived **subscription
token** instead:

1. On your machine, run `claude setup-token`. It opens a browser to sign in and prints a token.
2. In the portal, paste it under **Settings > Provider keys > Claude subscription token** - in the
   global settings for every project, or in a project's settings for that one.

The token is stored encrypted like an API key, shown again only as its last four characters, and
handed only to the `claude` CLI (as `CLAUDE_CODE_OAUTH_TOKEN`): to a scan's child process when
`claude-cli` describes its commits, and to the portal itself for rationale cards. It never reaches
CodeGraph, git, logs or your repository. Each call runs `claude` with a private, throw-away profile
directory, and the CLI's auto-updater is off.

Outside the portal the same token works for headless `whygraph scan` - set
`CLAUDE_CODE_OAUTH_TOKEN` in the environment (a CI secret), or `oauth_token` in a gitignored
`[llm.claude_cli]`. `config_dir` does not apply in the portal.

A scan configured for `claude-cli` that cannot run it skips the LLM descriptions phase with one
message instead of failing every commit: "claude-cli needs your Claude subscription token" in the
portal without a token, "claude CLI not found on PATH" natively without the CLI.

### `ollama`

Local models, no credential. Point it at your daemon:

```toml
[llm]
model = "ollama/llama3"

[llm.ollama]
# host = "http://localhost:11434"
timeout_sec = 120
```

Chat cannot use `ollama`; with `[llm].model` on Ollama, chat falls back to `anthropic`.

**From the portal**, `localhost` is the container, so set the Ollama host to
`http://host.docker.internal:11434` (under Endpoints in Settings) to reach a daemon on your machine. The
same name reaches any OpenAI-compatible gateway on the host: set the OpenAI base URL to
`http://host.docker.internal:<port>/v1`. Changing the OpenAI base URL clears the key stored for the old
one.

Timeouts default higher than the hosted providers because local inference is slower.

## When a provider is misconfigured

It depends on the role:

- **Analysis** degrades gracefully. A missing key means commits get no LLM description; the scan
  still records the full git and GitHub history, and descriptions backfill lazily once the key works.
- **Rationale** degrades gracefully. Evidence tools keep working; the rationale card is what you lose.
- **Chat** fails per turn, loudly. There is nothing useful to return without a model, so the turn
  surfaces an error naming the environment variable it needs.

## Adding a provider

The adapter registry is an extension point - `LlmClientFactory` has a `register()` method, so a new
adapter can be added without modifying the factory itself. WhyGraph ships the six above; anything else
is your own code, and unknown provider tags surface as an error when the client is constructed, not
when the config is parsed.
