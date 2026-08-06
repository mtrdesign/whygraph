# Roadmap

WhyGraph works today for the core loop - scan a repo, serve evidence and rationale over MCP - plus a
local web panel with an Explorer and a chat assistant over the same data. Here's what shipped
recently, and what's planned but not yet built. Treat the planned items as direction, not a promise
of dates.

## Recently shipped

| Feature | What it gives you |
|---|---|
| [Explorer](guide/playground.md) | A local web panel over the graph, evidence, and rationale. |
| [Chat assistant](guide/chat.md) | Ask questions in English; it calls WhyGraph's tools, runs aggregate SQL, and charts the results. |
| [Author identity](guide/concepts.md#people) | One row per human, resolved from mailmap and GitHub rather than guessed. |
| [Branch membership](guide/scanning.md#how-whygraph-sees-branches) | Shipped history is distinguished from work in progress, recomputed and self-healing every scan. |
| [Auto-rescan git hooks](guide/scanning.md#keep-it-fresh) | The databases track your commits in the background, no daemon. |
| [Curl install](getting-started/installation.md) | A tag-pinned one-liner; the tag in the URL is the version you get. |

## More source-control providers

GitHub is the only supported remote today. These are on the way:

| Provider | Status |
|---|---|
| GitHub | Supported |
| Azure DevOps | Upcoming |
| GitLab | Upcoming |
| Forgejo | Upcoming |
| Others | Under consideration |

Until then, run against a GitHub remote or stay git-only with `[scan].provider = "off"`. See
[Providers](reference/providers.md).

## Deferred capabilities

Larger, net-new pieces that aren't built yet:

- **Cross-repo project registry** - orchestrate WhyGraph across several repos from one place.
- **Persistent / server mode** - a long-running endpoint with an HTTP MCP transport, so an app
  doesn't spawn a fresh stdio session per connection. This is what unlocks the full
  [service model](deploy/service.md).
- **Per-branch CodeGraph index** - the code index is still single-branch, so switching branches and
  re-syncing rewrites it. WhyGraph's *own* database no longer needs this: it keeps one database and
  [computes branch membership](guide/scanning.md#how-whygraph-sees-branches) per commit, recomputed
  and self-healing on every scan.

!!! info "Want to weigh in?"
    These are shaped by what people actually need. Open an issue on
    [GitHub](https://github.com/mtrdesign/whygraph) if one of these matters to you.
