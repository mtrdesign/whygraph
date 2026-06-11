# Roadmap

WhyGraph works today for the core loop — scan a repo, serve evidence and rationale over MCP. Here's
what's planned but not yet built. Treat everything below as direction, not a promise of dates.

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

- **Cross-repo project registry** — orchestrate WhyGraph across several repos from one place.
- **Persistent / server mode** — a long-running endpoint with an HTTP MCP transport, so an app
  doesn't spawn a fresh stdio session per connection. This is what unlocks the full
  [service model](deploy/service.md).
- **Per-branch databases** — separate CodeGraph and WhyGraph databases per branch, so a scan on one
  branch doesn't clobber another's.

!!! info "Want to weigh in?"
    These are shaped by what people actually need. Open an issue on
    [GitHub](https://github.com/mtrdesign/whygraph) if one of these matters to you.
