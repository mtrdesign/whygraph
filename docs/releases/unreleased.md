# Unreleased

Linked checkouts can now propose project notes to their platform and read the team's notes back.

!!! note "Not released yet"
    This page describes what is on `main` and will become the next release's notes.

## What's new

- **Propose notes from a linked checkout.** `whygraph_propose_note`, `/whygraph-note`,
  `/whygraph-claude-md` and the `/whygraph-implement` flows now work on a checkout [linked to a
  platform](../portal/platform-projects.md#notes). The proposal lands in the platform's review queue
  as an agent proposal from you, and the result carries a review link (a platform URL). The platform's
  Notes page shows the machine it came from to every member of the project. Anchors are narrowed to
  what is pushed (`anchor_downgraded`, `dropped_refs`); there is no offline queue.
- **Link-trusted notes reach linked agents.** Rationale cards, `whygraph://note/{id}` and the Claude
  Code hooks on a linked checkout carry the platform's notes that another member (or a project
  admin) confirmed. A note a non-admin confirmed themselves stays on the platform.
- **Hooks on linked checkouts.** The Claude Code hooks now install on linked projects too. For a
  checkout linked before this release, run **Update agent files** once from the portal to get them.
- **What leaves your machine.** Note text (body, check text and glob, area) is sent as written and can
  describe unpushed work; text that looks like a token or an AI provider key is refused, locally and
  on the platform. The connect consent screen now says
  that your local WhyGraph can propose notes. See [Security](../portal/security.md#project-notes).

## Upgrading

Update the **platform first**. A platform that has not been updated answers a linked checkout's note
proposal with `note_unsupported` and delivers no notes; the link stays `ok`. Nothing else needs
doing.
