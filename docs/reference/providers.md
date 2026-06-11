# Source control providers

WhyGraph's remote crawl enriches your scan with pull requests and issues. You choose the backend with
`[scan].provider` in `whygraph.toml`. Today there's one supported provider — GitHub — with others on
the way.

## GitHub — supported

Set the provider to `github` to pull PRs and issues from the GitHub remote:

```toml
[scan]
provider = "github"
```

Or let WhyGraph detect it from your remote URL:

```toml
[scan]
provider = "auto"     # detect from the remote (github only, for now)
remote = "origin"     # the git remote whose URL is inspected
```

The crawl uses the `gh` CLI, so you need it authenticated. Provide a token one of three ways:

- An existing `gh auth login`.
- `GH_TOKEN` or `GITHUB_TOKEN` in your environment.
- `[scan].token` in `whygraph.toml` — handy when one container scans repos across different orgs.

!!! note "Off by default"
    `provider` defaults to `"off"`, so a fresh scan stays git-only and needs no token. Opt into the
    remote crawl by setting `github` or `auto`. You can also skip it per-run with
    `whygraph scan --no-remote`.

## Upcoming

Other hosts are planned, not yet built. Until they land, point WhyGraph at a GitHub remote or run
git-only with `provider = "off"`.

| Provider | Status |
|---|---|
| GitHub | Supported |
| Azure DevOps | Upcoming |
| GitLab | Upcoming |
| Forgejo | Upcoming |
| Others | Under consideration |

See the [Roadmap](../roadmap.md) for the broader picture.
