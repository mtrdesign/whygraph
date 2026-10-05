# Roadmap

WhyGraph works today for the core loop - scan a repo, serve evidence and rationale over MCP - through
a local portal that holds all your projects, with an Explorer and a chat assistant over the same data. Here's what shipped
recently, and what's planned but not yet built. Treat the planned items as direction, not a promise
of dates.

## Recently shipped

| Feature | What it gives you |
|---|---|
| [The portal](portal/index.md) | One local server for all your projects: `whygraph up`, add repos from the browser, an HTTP MCP endpoint per project, scans run and tracked for you. |
| [Explorer](guide/playground.md) | A web view over the graph, evidence, and rationale. |
| [Chat assistant](guide/chat.md) | Ask questions in English; it calls WhyGraph's tools, runs aggregate SQL, and charts the results. |
| [Author identity](guide/concepts.md#people) | One row per human, resolved from mailmap and GitHub rather than guessed. |
| [Branch membership](guide/scanning.md#how-whygraph-sees-branches) | Shipped history is distinguished from work in progress, recomputed and self-healing every scan. |
| [Auto-rescan git hooks](guide/scanning.md#keep-it-fresh) | The portal tracks your commits in the background; hooks just ask it to rescan. |
| [Projects from a platform](portal/platform-projects.md) | Link a local checkout to a project on a production WhyGraph: your agent gets the team's history through your local portal while your uncommitted work never leaves your machine. |
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

Until then, run against a GitHub remote or stay git-only with `[scan].forge = "off"`. See
[Providers](reference/providers.md).

## Deferred capabilities

Larger, net-new pieces that aren't built yet:

- **Cross-repo queries** - the portal holds many projects, but each is still analyzed on its own;
  questions that span repos are not built.
- **A multi-user, remote portal** - local mode is single-user and loopback-only. A
  [production mode](deploy/production.md) with logins, sessions and organizations on their own
  subdomains, whose projects are imported from GitHub through a GitHub App, is built but unreleased.
  Agents reach its projects through a developer's [connected local portal](portal/platform-projects.md),
  but the production portal itself has no MCP endpoint: agents that talk to it directly (per-agent
  tokens, cloud agents) are what unlock the full [service model](deploy/service.md) beyond one
  machine.
- **Per-branch CodeGraph index** - the code index is still single-branch, so switching branches and
  re-syncing rewrites it. WhyGraph's *own* database no longer needs this: it keeps one database and
  [computes branch membership](guide/scanning.md#how-whygraph-sees-branches) per commit, recomputed
  and self-healing on every scan.

!!! info "Want to weigh in?"
    These are shaped by what people actually need. Open an issue on
    [GitHub](https://github.com/mtrdesign/whygraph) if one of these matters to you.
