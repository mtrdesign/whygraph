# Source control providers

WhyGraph's remote crawl enriches your scan with pull requests and issues. You choose the backend with
`[scan].forge` (a setting in the portal, or a key in a `whygraph.toml` the portal imports) (1.x called it `[scan].provider`; that spelling still works, with
a deprecation warning, until 3.0). Today there's one supported provider - GitHub - with others on
the way.

!!! note "Looking for the LLM providers?"
    This page is about **source-control hosts**. For the model adapters behind `[analyze]`,
    `[rationale]`, and `[chat]`, see [LLM providers](llm-providers.md).

## GitHub - supported

Set the forge to `github` to pull PRs and issues from the GitHub remote:

```toml
[scan]
forge = "github"
```

Or let WhyGraph detect it from your remote URL:

```toml
[scan]
forge = "auto"        # detect from the remote (github only, for now)
remote = "origin"     # the git remote whose URL is inspected
```

The crawl uses the `gh` CLI, inside the image.

**In the portal**, enter the GitHub token in the project's configure step (or when you add the
repository). It is stored encrypted, checked against GitHub when you enter it, and handed to the
scan's `gh` calls as `GH_TOKEN` for that scan only. `GH_TOKEN` and `GITHUB_TOKEN` in your shell do **not**
reach the portal. In [production mode](../deploy/production.md#projects) there is no token to enter:
the crawl uses the GitHub App's installation token, scoped to the one repository.

A `[scan].token` in a `whygraph.toml` is imported once into that encrypted token; it is never read at
scan time.

!!! note "Off by default"
    `forge` defaults to `"off"`, so a fresh scan stays git-only and needs no token. Opt into the
    remote crawl by setting `github` or `auto`. You can also skip it per run with **Quick rescan** in
    the portal, which reads git history and CodeGraph only.

    A Quick rescan also skips **PR-origin recovery**, which needs the network to fetch a
    squash-merged PR's original head. The same applies whenever no GitHub client resolves.

## Upcoming

Other hosts are planned, not yet built. Until they land, point WhyGraph at a GitHub remote or run
git-only with `forge = "off"`.

| Provider | Status |
|---|---|
| GitHub | Supported |
| Azure DevOps | Upcoming |
| GitLab | Upcoming |
| Forgejo | Upcoming |
| Others | Under consideration |

See the [Roadmap](../roadmap.md) for the broader picture.
