# LLM providers

WhyGraph calls an LLM in three places, and each picks its provider independently:

| Role | Config | What it does |
|---|---|---|
| Analysis | `[analyze] provider` | Writes a per-commit description of the diff during `whygraph scan`. |
| Rationale | `[rationale] provider` | Writes the five-section rationale card, for MCP, the Explorer, and chat. |
| Chat | `[chat] provider` | Drives the [chat assistant](../guide/chat.md) in `whygraph serve`. |

Six adapters ship. All six can fill the analysis and rationale roles; only four can drive chat,
because chat needs streaming tool calls.

| Provider | Config section | Credential | Analysis / rationale | Chat |
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

Two tables, and they do different jobs. The **role** table says *which* adapter to use; the
`[llm.*]` table says *how* to reach it.

```toml
[rationale]
provider = "anthropic"        # which adapter
# model = "claude-haiku-4-5"  # override this role's model only

[llm.anthropic]               # how to reach that adapter
model = "claude-opus-4-7"     # the provider's default model for every role
# api_key = "sk-ant-..."      # default: read ANTHROPIC_API_KEY from env
timeout_sec = 60
```

Leave a role's `model` unset and it uses the provider's own `[llm.*]` model. Set it to give that one
role a cheaper or stronger model than the rest - a common setup is a fast model for per-commit
analysis and a stronger one for rationale.

Omit `api_key` and the adapter reads the conventional environment variable. That is the recommended
setup: `whygraph.toml` is gitignored, but keys in the environment cannot leak into a commit at all.

See [Configuration](configuration.md) for every key.

## Notes per provider

### `openrouter`

`model` defaults to `openrouter/auto`, which routes your request automatically. That is fine for
analysis and rationale, but **not every routed model supports tool calling** - pin a specific
tool-capable model when using OpenRouter for chat:

```toml
[llm.openrouter]
model = "anthropic/claude-sonnet-4"
```

### `claude-cli`

Runs through your local Claude Code CLI and bills against your **subscription** rather than an API
key. To make that work it deliberately strips `ANTHROPIC_API_KEY` from the subprocess environment, so
having that variable set for other providers does not silently switch you to metered API billing.
Setting `api_key` in `[llm.claude_cli]` explicitly puts it back - that is the opt-in to API billing.

!!! warning "The tag and the section name differ"
    The provider tag is hyphenated but the config section is not:

    ```toml
    [rationale]
    provider = "claude-cli"   # hyphen

    [llm.claude_cli]          # underscore
    model = "claude-opus-4-7"
    ```

    Both `[llm.claude_cli]` and `[llm.claude-cli]` parse, so either spelling of the section works.
    The `provider` value is always `"claude-cli"`.

### `ollama`

Local models, no credential. Point it at your daemon:

```toml
[llm.ollama]
model = "llama3"
# host = "http://localhost:11434"
timeout_sec = 120
```

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
