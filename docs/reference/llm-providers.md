# LLM providers

WhyGraph calls an LLM in three places. All three default to `[llm].model`, and each can override it:

| Role | Config | What it does |
|---|---|---|
| Analysis | `[analyze]` | Writes a per-commit description of the diff during `whygraph scan`. |
| Rationale | `[rationale]` | Writes the five-section rationale card, for MCP, the Explorer, and chat. |
| Chat | `[chat]` | Drives the [chat assistant](../guide/chat.md). |

Five adapters ship. All five can fill the analysis and rationale roles; only four can drive chat,
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

`ollama` is excluded from chat because local models' tool-calling reliability varies too much to
depend on.

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
never written into your repository. Each provider has a key card there: project admins (and owners, on
the defaults) see when the key was last used and can **Test** it. A test asks the provider whether the
stored key works without spending tokens - Anthropic's, OpenAI's and DeepSeek's model listings, and
OpenRouter's key endpoint (its model listing needs no key, so it would prove nothing); a custom base
URL is tested at that URL. `ollama` has no key to test. Tests are limited to 10 per person and 60 per
organization an hour. For headless `whygraph scan`, omit `api_key` and the adapter reads
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

### `claude-cli` (removed)

Earlier builds had a `claude-cli` provider that ran the Claude Code CLI on a Claude subscription
token. It is gone: Anthropic's terms do not allow third-party tools to collect or store Claude
subscription credentials, or to route requests through Free, Pro or Max plan credentials (see
[Authentication and credential use](https://code.claude.com/docs/en/legal-and-compliance)). Use the
`anthropic` provider with an API key instead, or another provider such as `openrouter`.

A leftover `claude-cli` setting is dropped when the config loads, with a warning, so that role falls
back to the next layer or the default provider: an `[llm.claude_cli]` or `[llm.claude-cli]` table, an
`[llm].model` of `"claude-cli/..."`, a role's `provider = "claude-cli"` (with that role's `model`),
and a role's `model = "claude-cli/..."`.
Importing a repository whose `whygraph.toml` still has one lists it in the preview's warnings. The
portal no longer stores a Claude subscription token: upgrading deletes any it held and clears
`claude-cli` from its stored settings.

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

## What each provider reports

The portal records every successful call in its [usage ledger](../portal/usage.md): the tokens the
provider reported, and a cost. What a provider reports decides how that cost is made.

| Provider | Tokens recorded | Cost |
|---|---|---|
| `anthropic` | Input (cache reads and cache writes included), output, and the cache read and write counts | **Estimated** from the price table |
| `openai` | Input, output, cached input, and reasoning tokens when the model reports them | **Estimated** from the price table |
| `deepseek` | Input, output, cache hits, and reasoning tokens when reported | **Unpriced** unless your organization adds a price: the bundled table has no DeepSeek rows, because its model names are retired and reused |
| `openrouter` | Input, output, cache reads and writes, reasoning tokens | **Provider-reported**: the `cost` OpenRouter charged. On a BYOK request OpenRouter's own charge excludes the upstream provider's, so WhyGraph adds the upstream inference cost to it |
| `ollama` | Input and output tokens only | **Unpriced**: a local model has no bill |

Three rules sit on top of that:

- **A cost the provider reports wins.** Only OpenRouter reports one. Every other priced call is
  `estimated`: its tokens times the price for that model, with cache reads and writes at their own
  rates (a missing cache rate falls back to the input rate).
- **A model with no price is `unpriced`.** Its tokens still count, its cost does not, and it is
  not counted toward [budgets](../portal/usage.md#budgets-and-the-hard-stop). An organization can add a
  price for it on the Usage & cost page's Prices tab.
- **A custom endpoint is never priced from the bundled table.** If a provider's `base_url` or `host`
  is changed (an OpenAI-compatible gateway, an Ollama daemon, a proxy), the other end may be a
  different service with different rates, so only an organization's own price override applies. An
  OpenRouter id such as `anthropic/claude-sonnet-4` is priced by the model behind it when no cost was
  reported.

## When a provider is misconfigured

It depends on the role:

- **Analysis** degrades gracefully. A missing key means commits get no LLM description; the scan
  still records the full git and GitHub history, and descriptions backfill lazily once the key works.
- **Rationale** degrades gracefully. Evidence tools keep working; the rationale card is what you lose.
- **Chat** fails per turn, loudly. There is nothing useful to return without a model, so the turn
  surfaces an error naming the environment variable it needs.

## Adding a provider

The adapter registry is an extension point - `LlmClientFactory` has a `register()` method, so a new
adapter can be added without modifying the factory itself. WhyGraph ships the five above; anything else
is your own code, and unknown provider tags surface as an error when the client is constructed, not
when the config is parsed.
