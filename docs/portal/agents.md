# Connecting agents

Each project has two MCP endpoints on the portal: `http://127.0.0.1:<port>/mcp/<slug>`, WhyGraph's own
server, and `http://127.0.0.1:<port>/mcp/<slug>/code`, CodeGraph's tools for the same project, served by
the portal (see [The CodeGraph endpoint](../reference/mcp.md#the-codegraph-endpoint)). When you
[set up a project](projects.md#set-up) and select agents, the portal writes two config entries
into each agent's project file, `whygraph` and `codegraph`, each pointing at its endpoint over HTTP.
Your agent then has the same [tools, resources and prompts](../reference/mcp.md) it always had; the
server name is unchanged, so `mcp__whygraph__*` tool names still work, and the graph tools are
`mcp__codegraph__*`.

The portal must be running for an agent to reach it - there is no per-session process to launch any
more.

!!! note "Linked projects use the same files"
    A checkout [linked to a platform](platform-projects.md) gets exactly the same entry and assets as a
    local-folder project, with the platform project's slug in the URL, so teammates linked to the same
    project share one committed entry. The agent still talks to **your local portal**, never to the
    platform: the local portal answers from your working tree and the platform's history.

## What gets written

All four agents are project-scoped: the file lives in your repository, so teammates can share it.

| Agent | Config file | Entries |
|---|---|---|
| Claude Code (`claude`) | `.mcp.json` | `"whygraph": {"type": "http", "url": "http://127.0.0.1:${WHYGRAPH_PORT:-8765}/mcp/<slug>"}` and `"codegraph"` with the same shape and `/mcp/<slug>/code`, under `mcpServers` |
| Cursor (`cursor`) | `.cursor/mcp.json` | `"whygraph": {"url": "http://127.0.0.1:8765/mcp/<slug>"}` and `"codegraph": {"url": "http://127.0.0.1:8765/mcp/<slug>/code"}` under `mcpServers` |
| VS Code / Copilot (`vscode`) | `.vscode/mcp.json` | `"whygraph": {"type": "http", "url": "http://127.0.0.1:${input:whygraph-port}/mcp/<slug>"}` and a `"codegraph"` entry with `/code`, under `servers`, plus one `inputs` entry that prompts for the port (both use it), default `8765` |
| Codex (`codex`) | `.codex/config.toml` | `[mcp_servers.whygraph]` with `url = "http://127.0.0.1:8765/mcp/<slug>"` and `[mcp_servers.codegraph]` with `/code`; both tables carry `tool_timeout_sec = 120` |

Both entries are written together, so a file gets one backup, one diff and one confirmation for the
pair. The Codex timeout is there because a CodeGraph call may wait for the index to catch up (see
[Freshness](../guide/mcp-usage.md#freshness)).

Each agent also gets its bundled assets - skills, commands and subagents that teach it when to call
WhyGraph - copied into `.claude/`, `.cursor/`, `.github/` (with the planning files in `.github/whygraph/`)
or the repo root and `.codex/agents/` (with the planning files in `.codex/whygraph/`), and a
CodeGraph usage-guidance block appended to its always-on instructions (`CLAUDE.md`, `AGENTS.md`,
`.github/copilot-instructions.md`, or an always-apply rule for Cursor). The guidance tells the agent to
prefer the portal's `codegraph_*` tools over grep, and not to run the `codegraph` command itself.

Claude Code and Cursor also get `skills/whygraph-ui-check/` (`.claude/skills/` and `.cursor/skills/`): a
slash command and the `capture.mjs` script behind the planning command's
[UI evidence](../guide/planning.md#ui-evidence). VS Code and Codex get the script beside their
planning files, in `.github/whygraph/` and `.codex/whygraph/`.

### Keeping the agent files up to date { #agent-files-upgrade }

The portal records what it installed in `.whygraph/assets.json` (gitignored with the rest of
`.whygraph/`): the WhyGraph version and a hash of every file it wrote, and, for the block in a shared
file such as `AGENTS.md`, of the text between the markers. A manifest committed to git is ignored, as
if there were none (a repository must not be able to name its own files as WhyGraph's). **Update agent
files** in the project's Settings (and re-initializing) uses it per file:

| The file on disk | Result |
|---|---|
| Missing | Written |
| Still the way WhyGraph wrote it | Updated silently to this version's copy (or your override) |
| You edited it, and this version's copy (or your override) is the same as the one you started from | Left alone |
| You edited it, and this version's copy (or your override) changed | Left alone and listed as **Edited**, with the difference |
| Installed before the manifest existed (a teammate's checkout, an older release) | Treated as edited |
| An [override](#agent-files-overrides) exists for it | Installed from the override and listed **Overridden** |
| Switched off in `.whygraph-overrides/disabled`, and not edited | Removed (listed as **Remove**) |
| Switched off in `disabled`, and you edited it | Left alone and listed as **Edited**, with what removing it would delete |
| Its override was removed | Back to WhyGraph's copy (a checkout without a manifest asks first) |
| No longer shipped by WhyGraph, and still the way WhyGraph wrote it | Removed (listed as **Remove**), with its folder when that is left empty |
| No longer shipped by WhyGraph, and you edited it | Left alone and listed as **Edited**, with what removing it would delete |

Tick **Overwrite my edits too** (shown only when edited files are listed) to replace those as well, and
to remove the edited files WhyGraph no longer ships. A file WhyGraph no longer ships is recognized from
the manifest, or, for a project initialized before the manifest existed, from the exact content of
every release that shipped it (Cursor's old `.cursor/commands/` files, for example). A symbolic link
is never followed or removed.
Settings shows `Agent files: v<installed> (current v<package>)` with an **Update** button when the
recorded version is behind, and the project's Overview lists "Agent files are out of date" the same way.
Removing an agent deletes only its MCP entry; the installed files and the manifest stay as they are.

### Overriding the bundled files { #agent-files-overrides }

Editing an installed copy works, but it is listed **Edited** after every release that touches the file,
and a teammate's fresh clone lists every committed file as edited. To change, extend or switch off a
bundled file for good, put the change in `.whygraph-overrides/` at the repository root instead. It is a
normal committed folder, so the whole team gets the same files, and **Update agent files** installs
them without asking. The portal reads it and never writes to it.

```text
.whygraph-overrides/
  disabled                                          # optional: one path per line
  claude-code/
    skills/whygraph-plan/sources/repo-tests.md      # replaces that bundled file
    agents/planner.append.md                        # appended to agents/planner.md
    CLAUDE.md                                       # replaces the WhyGraph block in CLAUDE.md
  vscode/
    agents/planner.agent.append.md                  # appended to agents/planner.agent.md
  codex/
    .codex/agents/planner.toml                      # replaces that bundled file
```

Three operations, each a plain file:

- **Replace.** A file at the same path as a bundled one. The installed file is your copy.
- **Append.** `X.append.md` beside a bundled `X.md` (or `X.append.mdc` beside `X.mdc`). The installed file
  is the bundled copy, a blank line, then your text, so it follows WhyGraph's own changes to the rest
  of the file. Append only works on `.md` and `.mdc` files. The last suffix is the one that counts: the
  VS Code file `agents/planner.agent.md` is appended to by `agents/planner.agent.append.md`.
  An append goes on top of a replacement of the same file when both exist.
  An append may also start with a small frontmatter that adds tools (below).
- **Disable.** List a path in `.whygraph-overrides/disabled`, one per line (blank lines and `#` comments
  are fine). The file is not installed, and an installed copy is removed by the usual rules for a file
  WhyGraph no longer ships (below). A path ending in `/`, or naming a folder, switches off everything
  under it; `claude-code` alone is the whole agent, except the shared instructions file, which cannot be
  switched off.

```text
# <overlay folder>/<path inside the agent's bundled tree>
claude-code/skills/whygraph-plan/sources/best-practices.md
cursor/skills/whygraph-plan/sources/best-practices.md
```

The path is the file's path under its overlay folder, which is the install path with the install folder
dropped and the overlay folder put in front of it:

| Agent | Overlay folder | Installs to | Shared instructions file (replace or append to its block) | Example overlay path |
|---|---|---|---|---|
| Claude Code | `claude-code/` | `.claude/` | `CLAUDE.md` | `claude-code/agents/planner.append.md` for `.claude/agents/planner.md` |
| Cursor | `cursor/` | `.cursor/` | none | `cursor/skills/whygraph-plan/sources/repo-tests.md` for `.cursor/skills/whygraph-plan/sources/repo-tests.md` |
| VS Code | `vscode/` | `.github/` | `copilot-instructions.md` | `vscode/whygraph/sources/repo-tests.md` for `.github/whygraph/sources/repo-tests.md` |
| Codex | `codex/` | the repository root | `AGENTS.md` | `codex/.codex/agents/planner.toml` for `.codex/agents/planner.toml` |

For a shared instructions file the override applies to the text between WhyGraph's markers only; the rest
of the file is yours. Overrides for an agent you did not select are ignored without a warning.

**Rules**

- Only files WhyGraph ships can be overridden. A path that matches none is reported as a probable typo
  and not installed. To add a file of your own, write it into the agent's folder as before.
- An override cannot contain WhyGraph's block markers (`BEGIN`/`END` comments of a shared file).
- Limits: 64 KiB per file, 100 entries (folders count), 1 MiB in total, 8 folders deep. Files must be
  UTF-8 text; a line-ending style or a leading byte-order mark is normalized.
- A symbolic link, or an entry the portal cannot read, is skipped. If the overrides folder, one of its
  agent folders or `disabled` cannot be read at all, **every file of the affected agent is left as it
  is**. A problem with an override never puts the bundled copy back over your override, and never
  re-installs a file you switched off: the installed file stays as it was.
- Frontmatter is not body text. Only `tools` / `allowed-tools` can be added through an append; anything
  else in frontmatter needs a replacement, and then you also own every later change WhyGraph makes to
  that file.

**Granting tools through an append.** To give an agent one more tool, such as an MCP server for a
research source, start the append with a frontmatter holding the tools to add:

```markdown
<!-- .whygraph-overrides/claude-code/agents/researcher-external.append.md -->
---
tools: mcp__medium__*
---
```

The installed `researcher-external.md` keeps WhyGraph's own `WebSearch` and gets every later WhyGraph
update, because the file is still the bundled one with your tools added. The rules:

- The frontmatter is the first thing in the append: a line that is exactly `---`, then lines, then a
  closing `---`. It may hold `tools` and `allowed-tools`, each at most once, one `key: value` per line.
- A value is either the comma form (`a, b`) or the list form (`['a', 'b']` or `["a", "b"]`). A tool name
  is letters, digits and `_ . * / @ : -`, with at most one parenthesized argument such as
  `Bash(git log *)`; at most 200 characters, and 50 tools per key.
- WhyGraph's file (or your replacement of it) must have the same key in its own frontmatter. Your tools
  are added after its tools, skipping any already there, and the line is written in the file's own
  format. Nothing is ever removed from a tool list.
- Text after the closing `---` is appended as the body, as described above; with none, only the tool
  line changes.
- Any error ignores the **whole** append and installs WhyGraph's file as it is, with one
  `append_frontmatter` warning.

**Warnings.** Problems are listed in the Initialize preview and result, and never stop an update.

| Warning | Meaning |
|---|---|
| Skipped: a symbolic link, an unreadable entry, or a path that leaves the folder | The entry was not used |
| Skipped: larger than the size limit | Over 64 KiB, or a limit above was reached |
| Skipped: not UTF-8 text | The entry was not used |
| Matches no file this WhyGraph version ships | Probably a typo, or a misnamed folder (`claude/`, `.claude/`, `copilot/`) |
| WhyGraph no longer ships this file; your override is unused | The bundled file was removed in this version |
| Only .md and .mdc files can be appended to | An `.append` next to another kind of file is ignored |
| A shared instructions file cannot be switched off; replace or append to its block instead | The file stays installed |
| The overrides folder could not be read; these agent files were left as they are | Nothing was changed for that agent |
| Skipped: contains WhyGraph's block markers | The file is left as it is |
| Append ignored: *reason* | The tools frontmatter of an append was refused (an unclosed block, an unknown or repeated key, a bad line or tool name, too many tools, or WhyGraph's file has no such key); the whole append is ignored |
| Switched off, but still used: *path* is used by *path* | See below |
| This file is switched off in disabled; the override is unused | The file stays off |
| More warnings were found than shown | At most 50 are listed |

**Files that belong together.** The planning command is one feature spread over several files: the
`whygraph-plan` skill, its agents, `plan-spine.md`, `guard.sh` and `plan-tool.sh`, and the UI-capture
script that planning and implementing both run (`skills/whygraph-ui-check/capture.mjs`, or the copy
beside the planning files). Switching one of them off is allowed, but when an installed file still names
its install path you get a **still used** warning with the first file that does. Nothing is refused, so
you can break the planner by accident; read the preview before you update. A reference by bare name,
such as an agent that says "the file plan-spine.md", is not detected. In particular, `guard.sh` is the
`PreToolUse` hook of the implementor, synthesizer and reviewer agents: without it the hook command
fails and Claude Code lets the tool call through, so those agents lose their guard. To turn UI capture
off, use `--no-capture` instead of removing the script.

**Moving an in-place edit into the overrides folder.** Copy your edited file to its overlay path (or, for
a shared file such as `AGENTS.md`, only the text between the markers), then run **Update agent files**.
Do it before you upgrade when you can: the portal then also shows what WhyGraph changed in that file
since you wrote the override.

**When WhyGraph changes a file you replaced.** The file is listed **Overridden** and keeps your copy.
The preview adds a notice, "WhyGraph changed this file since your override was written", with the
difference between WhyGraph's old and new copy (kept in `.whygraph/asset-bases/`), or, when the old copy
is not kept on this machine, your override against WhyGraph's current one. It is a notice, not a
question; tick **Update agent files** to dismiss it.

**Workflow.**

- Run **Update agent files** before you commit a change to `.whygraph-overrides/`, and commit the
  installed files with it. After pulling a change to `.whygraph-overrides/`, run **Update agent files**.
  An **Edited** row on a fresh clone can mean the committed installed copy is older than the override.
- A replacement is **Up to date** on every teammate's checkout. An append, or a file with no override,
  is only so when the teammate runs the same WhyGraph version as the person who committed it.
- Prefer an append to a replacement, and a research source in `sources/` to a rewritten planner. An
  append keeps up with WhyGraph; a replacement has to be rebased by hand.
- Review `.whygraph-overrides/` like code, after a clone and in pull requests. Replacing a script
  (`*.sh`, `*.mjs`), an agent's or skill's frontmatter (`hooks`, `tools`, `allowed-tools`), or adding
  tools to it through an append,
  `whygraph-plan/SKILL.md` (which holds the [consent rules](../guide/planning.md#consent)) or a Codex
  agent file changes what agents may do. A replaced bundled source keeps its bundled name, so the
  planning command runs it without asking, exactly as it would an edited one.

## Safe to commit

Every one of these files may be committed, and so may `.whygraph-overrides/` (see [Overriding the bundled files](#agent-files-overrides)). On the default port (`8765`) the entry is identical for
every teammate, because it carries no path and no machine name:

- Claude Code and VS Code read the port from an **environment variable** or a **prompt**, with `8765` as
  the default.
- Cursor and Codex do not document a default for an unset variable, so their entries carry the port
  **literally**. An unset variable would otherwise drop the port and break the entry for everyone.

The slug in the URL comes from the repository's name, so teammates normally end up with the same one.
If another project already had that name on your portal, the slug gets a numeric suffix and the entry
is specific to your machine; the preview tells you when that happens.

The planning command's research sources are files in your repository (`.claude/skills/whygraph-plan/sources/`
and the matching folder for each agent), so a clone may carry one you did not write. The command asks
before it runs any source that was added to the repository or reaches an outside service, including the
bundled web search source `best-practices.md`, but review
`sources/` and `.whygraph-overrides/` after cloning, and in pull requests. Codex has no per-agent tool limit, so there the rules in the researcher's own
text are the only control; see [Planning a change](../guide/planning.md#differences-between-editors).

If an agent file is already tracked by git, the portal shows a diff first and writes it only after you
confirm. If it cannot parse the file safely - JSONC with comments, or TOML with comments - it leaves the
file alone and shows the entry to paste yourself. Before any rewrite, the old file is copied to
`.whygraph/backups/`.

## A non-default port

If you started the portal with `--port`, the entries written at that time already carry it. When you
change the port later, the portal rewrites the markers in your repositories itself, and follows the
agent entries by what each agent supports. Both entries (`whygraph` and `codegraph`) follow the same
way, so each item below is one line per entry:

| Agent | What to do after a port change |
|---|---|
| Claude Code | Export `WHYGRAPH_PORT=<port>` in the environment you start Claude Code from. The committed file needs no edit |
| VS Code | Enter the new port when the editor prompts for it |
| Cursor | Edit the port in `.cursor/mcp.json`. The portal rewrites it for you when the file is not tracked by git |
| Codex | Edit the port in `.codex/config.toml`. The portal rewrites it for you when the file is not tracked by git |

A file tracked by git is **never rewritten automatically** on a port change - committed configs
belong to the whole team. After a port change the **Projects** page shows a notice listing what the
portal changed, the exact lines to change by hand in each tracked file (the `whygraph` line, then the `codegraph` one;
with a copy button), the
environment hints for Claude Code and VS Code, and any project folders it could not reach (they still
name the old port until the portal starts with the folder available). Each project's overview and
settings show that project's part. Dismissing a notice hides it until the port changes again. The
same report is in the `port_change` field of `GET /api/portal/state` and of
`GET /api/projects/<slug>`.

## Agent-specific notes

**Claude Code** asks you to approve a project-scoped MCP server the first time it sees it. Approve
both `whygraph` and `codegraph` when prompted (or with `/mcp`); nothing connects until you do. After
**Update agent files** adds the `codegraph` entry to a project you set up earlier, Claude Code asks
again for the new server, unless you once chose to approve all future servers in the project (`claude mcp
reset-project-choices` forgets that choice). Each `mcp__codegraph__*` tool also asks for permission the
first time in a project. To allow them without the prompt, add `"mcp__codegraph__*"` to
`permissions.allow` in the untracked `.claude/settings.local.json`; the portal does not write that file.

**VS Code** also reads a workspace-root `.mcp.json`. A repository configured for both Claude Code and
VS Code therefore lists the `whygraph` and `codegraph` servers twice in VS Code. That is noisy, not
broken; disable one of each pair.

**Codex** loads `.codex/config.toml` only for projects you have marked as trusted. Trust the project
in Codex for both servers to load.

## Claude Code hooks { #claude-code-hooks }

For Claude Code the portal also installs two [hooks](https://docs.claude.com/en/docs/claude-code/hooks)
that put your team's confirmed [project notes](../guide/notes.md) in front of the agent without it
having to ask:

- **SessionStart** delivers the project-wide notes and one line on how fresh the index is (up to date,
  or how many commits behind HEAD).
- **PostToolUse**, for `Read`, `Edit` and `Write`, delivers the confirmed notes that apply to that
  file, once per session and agent.

Every note is labelled as written and confirmed by project members, data and not instructions. A
proposed note is never delivered; see [Project notes](../guide/notes.md).

**Where they live.** **Initialize** and **Update agent files** add them to `.claude/settings.json`,
next to the other Claude Code files, and write a small helper, `whygraph-agent-hook`, in the git
directory beside the auto-rescan helper (`.git/whygraph/`), not in the working tree. The portal never
writes `.claude/settings.local.json`; the permission note under [Agent-specific
notes](#agent-specific-notes) is still yours to add there. The preview lists `.claude/settings.json`
like any other file.

- If git tracks `.claude/settings.json`, the portal shows a diff and writes only after you confirm.
  Declining leaves the hooks uninstalled; Initialize still completes, and everything else is written.
- Only an entry of exactly the shape the portal writes is its own. Any other entry in the file, yours
  or a teammate's, is never edited or removed, and one that merely looks like ours (it runs
  `whygraph-agent-hook` but differs) is reported to you and left alone.
- A file with comments, or one that does not parse, is refused with the snippet to paste yourself.
- Removing Claude Code from the project strips exactly the entries the portal wrote.

**Commit-safe.** The entries hold no slug and no port. Each is a short shell command that finds the
helper from `CLAUDE_PROJECT_DIR`, runs it if it is there, and always exits 0 with a 3 second limit. A
teammate who clones the repository without WhyGraph sees nothing and no error; with it, the helper
reads the project's slug and port from the untracked `.whygraph/portal.env` and asks your local portal
on `127.0.0.1`. If the portal is down, the helper prints nothing.

**Local mode only.** Production has no hooks, and a project [linked to a platform](platform-projects.md)
gets none.

**Limits.**

- A session whose `CLAUDE_PROJECT_DIR` is a linked git worktree or a subdirectory of the repository
  gets no notes.
- A hook input over 4 MiB gets none. Claude Code sends the whole file with an edit and the content
  twice with a write, so an `Edit` of a file over about 4 MiB or a `Write` over about 2 MiB gets no
  notes.
- A subagent receives a file's notes but not the session-start notes or the freshness line.
- `NotebookEdit` is not matched.
- The record of what was already delivered is in memory: restarting the portal delivers a file's notes
  again, and so does a `/compact`. After `/clear` the session starts over.
- A local process that can bind the portal's loopback port while the portal is down could answer the
  hook and inject context into a session. That is the same residual risk as the URL in `.mcp.json`;
  the helper still refuses any answer that is not the fixed shape (see
  [Claude Code hook output](security.md#hook-output)).

## Planning a change { #planning }

`/whygraph-plan <task>` writes a plan from research over your code graph and rationale, and
`/whygraph-implement <plan>` carries it out. See [Planning a change](../guide/planning.md) for the
pipeline, the flags and the research sources.

## Writing a decision down { #whygraph-note }

`/whygraph-note <what to remember>` proposes a [project note](../guide/notes.md) from your editor. It
is installed for Claude Code and Cursor (as a skill) and for VS Code (as a prompt under
`.github/prompts/`); Codex's `AGENTS.md` carries one paragraph telling the agent to call
`whygraph_propose_note`. The command asks for the anchor (a symbol, a file or lines, an area, or the
whole project) when the context does not make it obvious, calls `whygraph_propose_note`, and prints the
review link. The note then waits for a member to confirm it on the project's Notes page; nothing reads
it before.

The implementor subagent stays free of MCP tools. It runs one step at a time, each in a fresh run at the
step's own model, with a [review gate](../guide/planning.md#the-pipeline) after each verified step (Claude
Code) and a separate acceptance run at the end. Its final report now ends with a
**`## Decisions worth a note`** list: at most three decisions it made that deviate from the plan or
that a future reader needs and the code does not show, each as a kind, an anchor and a short
paragraph. `/whygraph-implement` in Claude Code and Cursor then proposes each one with
`whygraph_propose_note` and prints the note ids and review links. In VS Code the implement prompt runs as
the implementor, which has no WhyGraph tools, so it ends by telling you to run `/whygraph-note` for each
listed decision.

**Update agent files** brings an existing project up to date. In Claude Code, the `whygraph-implement`
skill is then allowed to call `whygraph_propose_note`, and nothing else from WhyGraph. It is also
allowed to run the plan tool, `plan-tool.sh`, which runs every `Verify` and Acceptance Check that a plan
names. In a permission mode that prompts, no prompt appears for those commands, so run only a plan you
reviewed.

## Reviewing a diff (Claude Code) { #whygraph-review }

Claude Code gets one more slash command, `/whygraph-review [<base>] [--generate]`. It checks a diff
against the recorded rationale: for each hunk it finds the enclosing symbol with CodeGraph, reads that
symbol's rationale card, and reports exactly one verdict - **Constraint at risk** or **Risk triggered**
(each quoting the card verbatim and ending with the item's source tag, such as `(quoted: 1a2b3c4d)` or
`(inferred)`), **No rationale**, or **Consistent** - followed by a
`## Confidence: high|medium|low` line. It runs as the `whygraph-reviewer` subagent, which may run only
`git diff` and writes no files.

- **What it reviews.** With no `<base>`, your uncommitted changes against `HEAD`; with a clean working
  tree, the branch against its merge base with the default branch (`origin/HEAD`, then `main`, then
  `master`). With a `<base>`, the working tree against the commit where your branch left it. Untracked
  files are new code and have no rationale, so they are left out; so are binary and deleted files. At
  most 40 hunks are reviewed.
- **No LLM spend by default.** It reads only cards that are already cached (`cached_only`), and a hunk
  without one reports **No rationale**. `--generate` lets WhyGraph generate the missing cards, one LLM
  call each, charged to the project's [budget](usage.md); a `budget_exceeded` refusal stops further
  generation for the rest of the review.
- **Cards survive moved and changed code.** A card is cached for a symbol's line range and the commits
  blamed on it. When the symbol has moved or changed length, the review still reads the card cached for
  an overlapping range of the same file, and when commits have landed on its lines since, it reads the
  earlier card and says so: that card is the rationale from before the change, which is what a review
  checks against. A hunk reports **No rationale** only when no cached card overlaps its symbol (see
  [Cached-only lookups](../reference/mcp.md#cached-only-lookups)).
- **Linked projects.** On a checkout [linked to a platform](platform-projects.md) the platform serves the
  cards and does not honour `cached_only`, so without `--generate` the reviewer does not ask for cards
  at all: every hunk with a symbol reports **No rationale** and confidence is `low`. The one evidence
  lookup it makes to tell that the project is linked may let the platform describe one commit, under
  the platform's description limit. With `--generate` it asks the platform, which may generate cards
  under its own budget.
- **Notes.** The reviewer reads the confirmed [project notes](../guide/notes.md) on a card, and a hunk
  that contradicts one is reported as a finding.
- **Other agents.** Cursor, VS Code and Codex do not get the reviewer yet. Cursor also loads
  `.claude/skills/`, so with Claude Code selected `/whygraph-review` shows up in Cursor's slash menu too;
  it is untested there.

## Evaluating an implementation { #whygraph-evaluator }

Claude Code and Cursor also get `whygraph-evaluator`, a read-only subagent that `/whygraph-implement`
starts beside the reviewer. The reviewer checks a diff against recorded rationale; the evaluator checks
the work against the plan: each step's scope and `Verify` command, then the quality of the whole change,
then the screens when [UI evidence](../guide/planning.md#ui-evidence) was captured. Skip it with
`--no-eval`. See [Evaluation](../guide/planning.md#evaluation) for the stages and for what your model
provider sees.

## Connecting from the Projects page { #connect-your-agent-outside-a-project }

You do not need to open a project first. On a production portal the **Connect local WhyGraph** button on
the Projects page checklist, and on the banner a new member sees, opens a short dialog: install
WhyGraph and run `whygraph up` on your machine, choose one of the projects you can read (a lone project
is chosen for you), then follow the **Connect local WhyGraph** card for it. In an organization with no
project, the checklist step links to importing a repository instead, and the banner offers no connect
button until you can read a project. A local portal's checklist sends you to the first
project's Overview instead, where the agent card lives.

## Agent activity

The portal counts how often agents call WhyGraph, per project, so the project's **Overview** can show
whether an agent is actually connected and what it asks for. What is counted:

- every MCP **tool call**, **resource read** and **prompt** on a project's `/mcp/<slug>` endpoint,
  under the tool, resource or prompt name (for example `whygraph_evidence_for` or `whygraph_commit`);
- every call to one of the four CodeGraph tools on `/mcp/<slug>/code` (`codegraph_explore`,
  `codegraph_node`, `codegraph_search`, `codegraph_impact`), which the Overview groups as **Code
  exploration**. A call to any other name is refused and never counted;
- on a production portal, every **data call** a [connected machine](platform-projects.md) makes for one
  of its agents (evidence, rationale, area history, a commit, a pull request, an issue, the repository
  overview).

A call counts even when the answer came from the cache. Protocol chatter (listing tools, resources or
prompts), the linked-project **status check** a local portal makes, a call refused by a rate limit,
and the Explorer and Chat views are not agent activity and are not counted.

Counts only: per project, day and call name, how many calls, how many ended in a tool error or an internal error, and their total duration - never an argument, a result or an error message. Each row also carries the project, the UTC day, the source (MCP or a connected machine), who made
the call and through which connection. Never a path, a symbol name, an argument or anything from the
answer. The names are WhyGraph's own or one of the four
CodeGraph tools. The Chat assistant's tool calls are counted the same way, by tool name per project, with source `chat`, no member and no connection, under **Chat tool calls**, and never their arguments; they are not agent calls and stay out of the agent totals. A call is counted when it ends, so a call still running when the portal shuts down is not counted.
A **tool error** is a call whose tool reported a failure (for example a target it could not find or a
refused argument); an **internal error** is a call where an exception escaped. A call's duration is
measured from the start of the tool body to its end; `v1:` data calls of a connected machine are
counted but not timed. Counts are written to the portal database
every 30 seconds, so a crash can lose at most the last 30 seconds of counts, and they are kept 400
days. A call that made an LLM request is also in the [usage ledger](usage.md), with its cost.

The Overview's Agent activity card also carries a line about the hosted CodeGraph server: its state,
process id and uptime, when the index was last refreshed and how long that took, and how many calls
waited for the index, timed out or were refused since the portal started. Those tallies live in memory
and are not part of the stored counts.

## Removing or changing agents

In the project's **Settings**, the Agents section reconfigures which agents are connected. Removing a
project can also strip the `whygraph` and `codegraph` entries the portal wrote, under the same backup
and tracked-file rules, and leaves every other server in those files alone. A `codegraph` entry that
is not the portal's own (one whose URL does not end in `/mcp/<slug>/code`) is never touched.

## Manual entry

If you would rather not let the portal edit a file - or it refused one - the entry to paste for each agent is shown
in the **Connect your agent** panel on the project's home page.

## If you installed CodeGraph yourself { #own-codegraph }

CodeGraph's own installer may already have wired your machine to a host-installed server. The portal
serves the same tools, pinned to each project, so you can drop the host install. Initialize looks for
what is left and reports it, and changes none of it without your word:

- **A `codegraph` entry of your own** (a stdio command, not the portal's URL) in an agent file is left
  exactly as it is, with a warning. Tick **Replace with the portal's CodeGraph** in the preview to swap
  it for the portal's entry.
- **CodeGraph's instruction section** (between its `CODEGRAPH_START` markers) in `CLAUDE.md`,
  `AGENTS.md` or `.github/copilot-instructions.md` is reported and left alone; delete it by hand once
  the portal's guidance block is in.
- **CodeGraph's own git hooks** (the sync hook in `post-commit`, `post-merge` or `post-checkout`) are
  reported and left alone. They are harmless but redundant with the portal's hooks.

To remove a global install, run `codegraph uninstall --keep-cli` on your machine: it removes the user
level server entry, the prompt hook and the instruction section and keeps the command. For Claude Code,
also run `claude mcp remove codegraph -s local` if you added the server for one project; a project
entry already shadows a global one. Codex reads a global `~/.codex/config.toml` too, and reads the
project's file only once you [trust the project](#agent-specific-notes).

To stop serving the tools from the portal while keeping your own server, turn **Serve CodeGraph tools
to agents** off in the project's Settings under Scanning (`[scan].serve_codegraph = false`). The tools
then answer with a "turned off" error and the Overview says so.

!!! warning "Do not run a host CodeGraph on the same repository"
    A second CodeGraph writing the same index is invisible to the portal's lock. While it holds
    CodeGraph's own file lock, the portal's refresh can report success without doing anything, so the
    portal's tools may answer from an index that is not current until the next change.

## Troubleshooting

- **The agent says it cannot connect.** Check `whygraph status`: the portal has to be running, on the
  port in the entry.
- **`409` from the endpoint.** The project is added but not initialized yet. Finish setup in the
  portal.
- **A CodeGraph tool says the project has no code index yet.** Run a scan from the project's Overview;
  the first scan builds the index. The tools never build one themselves.
- **A CodeGraph tool says the code index is still updating.** It names how many seconds to wait. Call
  the same tool again after that; the refresh keeps running in the background. See
  [Freshness](../guide/mcp-usage.md#freshness).
- **CodeGraph is paused on this portal after repeated crashes.** The one shared server stopped after
  crashing again and again, for every project. Use **Restart CodeGraph** on a project's Overview, or
  call again after the number of seconds the message names; **Open CodeGraph log** shows why it crashed.
- **The CodeGraph tools are turned off for this project.** `[scan].serve_codegraph` is `false`; turn it
  back on under Settings > Scanning.
- **CodeGraph is not available on this portal.** There is no `codegraph` command on the portal's `PATH`
  (only seen when you run the portal natively for development).
- **Indexing is refused because a symbolic link leaves the repository.** The message names the links.
  Remove them and scan again; see the [security model](security.md#symbolic-links).
- **A 1.x entry that runs `whygraph-mcp`.** That command was removed; it prints a message and exits. Add
  the repo in the portal and migrate the entry. See [Upgrading from 1.x](upgrading.md#from-1x).
