# Source control providers

WhyGraph's remote crawl enriches your scan with pull requests and issues. You choose the backend with
`[scan].forge` (a setting in the portal, or a key in a headless `whygraph.toml`) (1.x called it `[scan].provider`; that spelling still works, with
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

**In the portal**, enter the GitHub token in the project's configure step (or when you add a GitHub
repository by URL). It is stored encrypted, checked against GitHub when you enter it, and handed to the
scan's `gh` calls as `GH_TOKEN` for that scan only. `GH_TOKEN` and `GITHUB_TOKEN` in your shell do **not**
reach the portal.

**Headless** (`whygraph scan` outside the portal), provide a token one of three ways, checked in this
order - the first one found wins and is exported as `GH_TOKEN` for that scan's `gh` calls:

1. `[scan].token` in `whygraph.toml` - handy when one container scans repos across different orgs.
2. `GH_TOKEN` or `GITHUB_TOKEN` in your environment.
3. An existing `gh auth login`.

!!! note "Off by default"
    `forge` defaults to `"off"`, so a fresh scan stays git-only and needs no token. Opt into the
    remote crawl by setting `github` or `auto`. You can also skip it per-run with
    `whygraph scan --no-remote`.

    `--no-remote` also disables **PR-origin recovery**, which needs the network to fetch a
    squash-merged PR's original head. The same applies whenever no GitHub client resolves, whatever
    `--pr-origins` says.

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
