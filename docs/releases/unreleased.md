# Unreleased

!!! note "Not released yet"
    This page collects what is going into the next release. It moves into a numbered release page
    when that release is cut.

## What's new

- **Connect local WhyGraph.** On a platform, "Use with your agent" is now **Connect local WhyGraph**
  and "Connected portals" is **Connected machines**. The checklist and the welcome banner no longer
  offer it before there is a project to connect. The project's card now lists the machines you
  linked, and the port is a detail you change only when yours is not 8765.
- **Remembered platforms.** Linking from a local portal remembers the platforms you already use.
- **Checkout readiness when linking.** Linking a checkout says up front that the repository must be
  cloned into a shared folder, shows a ready-to-run clone command for each shared folder, finds a
  fresh clone on **Check again**, selects the only matching checkout, and warns before connecting
  when the project is already on this machine. A new link is confirmed with the platform at once,
  so a machine you just linked shows up on the project's card and is not revoked as unused.
- **One confirmation from the platform.** Starting a link from the platform's card needs one
  confirmation (**Connect** on your machine); the platform's Allow page is skipped. The card says
  what the link grants, and the approval it carries works once, for the same person, project and
  port, within 15 minutes.
- **Faster rescans.** After a repository's first scan, a scan asks GitHub only for pull requests and
  issues created or updated since the previous one, so *Describe now*, *Scan now* and GitHub push
  syncs no longer download the whole history again.

## Fixes

- **Linking with your organization's address.** Entering your organization's address (instead of
  the platform's) in **From a platform** no longer fails after you approve; the organization is
  preselected.
- **Pull requests and issues stay current.** A pull request that was open when first scanned and
  merged later is now updated (state, comments, closed issues). The first full scan after upgrading
  walks the GitHub history once more to bring older rows up to date.
- **Squash-merge recovery no longer stalls.** One pull request whose branch ref is gone from GitHub
  no longer stops every other squash-merged pull request from being recovered, and it is not retried
  on every scan.
