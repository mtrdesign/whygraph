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
- **Model suggestions.** Model fields in Settings suggest the provider's model ids while you type;
  any id can still be entered.
- **Chat follows your keys.** A new chat starts on the provider set for Chat when it has a key,
  otherwise on the first provider that has one, and the picker lists only providers with a key.
- **Edit and regenerate in Chat.** Edit a question, or regenerate any reply, and the earlier
  version stays: switch between versions with the `‹ 2/3 ›` arrows. **Copy** works on any message.
- **Branch from here in Chat.** A hover action on any question or reply starts a new chat with the
  conversation up to that message and opens it; the original stays as it was.
- **Continue a cut-off reply.** When a reply hits the output limit, **Continue** in the notice
  carries on from where it stopped, with no new question.
- **Effort level in Chat.** The **Effort** select beside the model picker sets how hard the model
  thinks, per chat, from the next reply. **Default** uses the level set for chats in Settings.
- **Steadier long replies.** A reply cut off by the output limit continues once, a model that goes
  quiet after its tool calls is asked for an answer, and older tool results in a long turn are
  shortened to fit. Anthropic prompt caching makes long tool-heavy turns cheaper, and a keepalive
  keeps slow replies connected through proxies.
- **Reasoning in Chat.** What the model reasons (Anthropic thinking summaries, DeepSeek and
  OpenRouter reasoning) shows in a collapsed **Reasoning** block above each step of a reply. On
  models where thinking is optional, such as Claude Opus 4.8, this turns thinking on.
- **Stop is immediate.** Stop ends the reply at once and keeps what was written, marked "Stopped."
  Sending right after a Stop waits for the stopped reply to end, then sends.

## Fixes

- **Chat turns that stopped without an answer.** A failed, empty or stopped reply no longer breaks
  every later question in the same chat. A reply that ends with no text says so instead of showing
  *Thinking…* forever. Long thinking no longer runs out of room or times out after 60 seconds (the
  wait is now `[chat].stream_timeout_sec`, 300 seconds by default). A dropped connection shows as
  the provider being unreachable, provider errors show their request id, and DeepSeek and OpenRouter
  reasoning models keep working across tool calls. After a turn reaches its tool-round limit, the
  final answer keeps the tools on the request with calls turned off.
- **Rolling back after this release.** This release adds columns to each project's WhyGraph
  database. An older image cannot open the upgraded database: to go back, restore each project's
  `backups/whygraph-<previous revision>.db`, which the portal writes beside its `whygraph.db` in
  the data directory before upgrading it.
- **Linking with your organization's address.** Entering your organization's address (instead of
  the platform's) in **From a platform** no longer fails after you approve; the organization is
  preselected.
- **Pull requests and issues stay current.** A pull request that was open when first scanned and
  merged later is now updated (state, comments, closed issues). The first full scan after upgrading
  walks the GitHub history once more to bring older rows up to date.
- **Squash-merge recovery no longer stalls.** One pull request whose branch ref is gone from GitHub
  no longer stops every other squash-merged pull request from being recovered, and it is not retried
  on every scan.
- **No key, no chat.** Without a stored key for any chat provider, New chat is disabled and messages
  are refused before they are saved, instead of failing after you send them.
- **Only stored keys count.** A portal run outside Docker ignores LLM keys and endpoints exported in
  its shell.
- **GitHub avatar in the sidebar.** The account menu inside an organization shows your GitHub
  picture.
- **Clearer import for the bootstrap admin.** A password account no longer sees Import buttons; the
  page explains how to invite a GitHub owner first.
