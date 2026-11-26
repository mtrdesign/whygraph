# Unreleased

Review discussion now reaches agents. The GitHub crawl stores pull request review summaries, review
threads and issue comments, and rationale cards use the review threads in which the author explains
code that still exists.

!!! note "Not released yet"
    This page describes what is on `main` and will become the next release's notes.

## What's new

- **Review discussion for agents.** `whygraph_evidence_for` and `whygraph_area_history` with
  `detail="full"` carry each pull request's `review_summaries`, the `review_threads` on the target's
  file (each with `current`, whether the code it was written about still exists), a count of the
  threads on other files, and issue `comments`. `whygraph://pr/{n}` and `whygraph://issue/{n}` carry
  them too, with every file's threads. See [MCP reference](../reference/mcp.md).
- **Backfill and thread pass.** The first GitHub scan after upgrading re-reads every pull request
  and issue once. Review threads are fetched by a resumable pass that stops at a rate-limit reserve
  and continues on the next scan, so a large repository can take several scans. See
  [Scanning](../guide/scanning.md).
- **Optional App events.** In production, subscribe the GitHub App to **Issue comment** and **Pull
  request review comment** to refresh this data sooner. Without them it still refreshes on the next
  scan.
- **Existing cards keep their content.** Nothing regenerates cards; they pick up review threads only
  when they are next generated.
- **Bot comments are gone.** Bot and hidden comments are no longer stored or shown, in cards, to
  agents or in Lead mode's PR last activity, except a bot's opening comment of a review thread a
  human answered.
- **Reviewer comments are opinions.** Only review threads in which the pull request's author explains
  code that still exists inside the target's lines reach a rationale card; review summaries and issue
  comments never do. The new `[rationale].review_threads_max_chars` caps that text, and
  `pr_discussion_max_comments` and `pr_comment_max_chars` now also bound full evidence. See
  [Configuration](../reference/configuration.md).
- **Smaller, cheaper rationale cards.** Each pull request appears once on a card, squash and merge
  commit bodies are no longer rendered, and the rest of the file's history is a compact list capped by
  the new `[rationale].context_max_chars`. The commit that created the lines is always kept, even when
  a target has many commits. A card's `coverage` says what was left out. See
  [Concepts](../guide/concepts.md#how-a-card-is-built).
- **Unrelated commits no longer invalidate a card.** One or two newer commits elsewhere in the file
  mark the card `context_stale` and it keeps serving; three or more regenerate it. `cached_only`
  returns `stale` (the lines changed) and `context_stale` separately. Existing cards keep serving
  and are replaced only when their evidence changes. Cards follow their symbol when the lines move.
- **Review threads need a unique anchor.** A thread appears on a card only when the code it was
  written about occurs once in the file.
