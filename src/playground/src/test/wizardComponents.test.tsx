import { useState } from "react";
import { act, render, screen, within } from "@testing-library/react";
import { RouterProvider, createMemoryHistory, createRootRoute, createRouter } from "@tanstack/react-router";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DetectedAgent, Detected, FileOutcome, InitResult } from "../api";
import { AgentPicker } from "../components/portal/AgentPicker";
import { DetectedPanel } from "../components/portal/DetectedPanel";
import { InitPreview } from "../components/portal/InitPreview";
import { NotSharedAlert } from "../components/portal/NotSharedAlert";
import { EstimateBody } from "../components/portal/ScanEstimateCard";

afterEach(() => vi.unstubAllGlobals());

// Components with a <Link> need a router context; a bare root route is enough.
async function renderWithRouter(ui: React.ReactNode) {
  const root = createRootRoute({ component: () => <>{ui}</> });
  const router = createRouter({
    routeTree: root,
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  await act(async () => {
    render(<RouterProvider router={router} />);
  });
}

// ---- multi-select agent picker ---------------------------------------------------

function PickerHarness({ initial = [], detected = [], removed = [] }: {
  initial?: string[];
  detected?: DetectedAgent[];
  removed?: string[];
}) {
  const [value, setValue] = useState<string[]>(initial);
  return (
    <>
      <AgentPicker value={value} onChange={setValue} detected={detected} removed={removed} />
      <output data-testid="value">{[...value].sort().join(",")}</output>
    </>
  );
}

describe("AgentPicker", () => {
  it("offers all four agents and selects several at once", async () => {
    const user = userEvent.setup();
    render(<PickerHarness />);
    const boxes = screen.getAllByRole("checkbox");
    expect(boxes).toHaveLength(4);
    for (const label of ["Claude Code", "Cursor", "VS Code / Copilot", "Codex"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }

    await user.click(screen.getByRole("checkbox", { name: /Claude Code/ }));
    await user.click(screen.getByRole("checkbox", { name: /Codex/ }));
    expect(screen.getByTestId("value")).toHaveTextContent("claude,codex");

    await user.click(screen.getByRole("checkbox", { name: /Claude Code/ }));
    expect(screen.getByTestId("value")).toHaveTextContent("codex");
  });

  it("shows the preselected agents as checked and badges detected ones", () => {
    const detected: DetectedAgent[] = [
      { agent: "cursor", file: ".cursor/mcp.json", key: "mcpServers.whygraph", shape: "stdio", stale: false, tracked: false },
    ];
    render(<PickerHarness initial={["cursor"]} detected={detected} />);
    expect(screen.getByRole("checkbox", { name: /Cursor/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Claude Code/ })).not.toBeChecked();
    expect(screen.getByText("detected")).toBeInTheDocument();
  });

  it("disables an agent whose entry is being removed instead", () => {
    render(<PickerHarness removed={["vscode"]} />);
    expect(screen.getByRole("checkbox", { name: /VS Code/ })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText("entry will be removed")).toBeInTheDocument();
  });
});

// ---- per-file preview states -------------------------------------------------------

function result(agent_files: FileOutcome[], asset_files: FileOutcome[] = []): InitResult {
  return {
    dry_run: true,
    gitignore_added: [],
    hooks: null,
    hooks_error: null,
    agent_files,
    asset_files,
    configured_agents: [],
    needs_confirmation: agent_files.filter((f) => f.status === "needs_confirmation").map((f) => f.file),
    refused: agent_files.filter((f) => f.status === "refused").map((f) => f.file),
    marker_written: false,
    initialized: false,
    custom_db_paths: [],
  };
}

const file = (over: Partial<FileOutcome> & Pick<FileOutcome, "file" | "status">): FileOutcome => ({
  agent: null,
  reason: null,
  snippet: null,
  diff: null,
  ...over,
});

describe("InitPreview", () => {
  const noop = () => {};

  it("renders a badge per status: create, update, up to date", () => {
    render(
      <InitPreview
        result={result([
          file({ file: ".mcp.json", status: "write", agent: "claude" }),
          file({ file: ".cursor/mcp.json", status: "overwrite", agent: "cursor", diff: "-old\n+new" }),
          file({ file: ".codex/config.toml", status: "skip", agent: "codex", reason: "already up to date" }),
        ])}
        selectedAgents={["claude", "cursor", "codex"]}
        confirmed={new Set()}
        onConfirmChange={noop}
      />,
    );
    const row = (f: string) => screen.getByTestId(`file-${f}`);
    expect(within(row(".mcp.json")).getByText("Create")).toHaveAttribute("data-status", "write");
    expect(within(row(".cursor/mcp.json")).getByText("Update")).toHaveAttribute("data-status", "overwrite");
    expect(within(row(".cursor/mcp.json")).getByText("Show changes")).toBeInTheDocument();
    expect(within(row(".codex/config.toml")).getByText("Up to date")).toBeInTheDocument();
    expect(within(row(".codex/config.toml")).getByText("already up to date")).toBeInTheDocument();
  });

  it("shows a refused file with a copyable snippet and never a confirm box", async () => {
    const user = userEvent.setup();
    const snippet = '{\n  "servers": { "whygraph": { "type": "http" } }\n}';
    render(
      <InitPreview
        result={result([
          file({
            file: ".vscode/mcp.json",
            status: "refused",
            agent: "vscode",
            reason: "contains comments",
            snippet,
          }),
        ])}
        selectedAgents={["vscode"]}
        confirmed={new Set()}
        onConfirmChange={noop}
      />,
    );
    const row = screen.getByTestId("file-.vscode/mcp.json");
    expect(within(row).getByText("Paste manually")).toHaveAttribute("data-status", "refused");
    expect(within(row).getByText(/will not touch this file/)).toBeInTheDocument();
    expect(row.querySelector("pre")).toHaveTextContent('"whygraph"');
    expect(within(row).queryByRole("checkbox")).toBeNull();

    await user.click(within(row).getByRole("button", { name: "Copy snippet" }));
    expect(await navigator.clipboard.readText()).toBe(snippet);
    expect(await within(row).findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("shows a tracked file with its diff and a confirm checkbox", async () => {
    const onConfirmChange = vi.fn();
    const user = userEvent.setup();
    render(
      <InitPreview
        result={result([
          file({
            file: ".mcp.json",
            status: "needs_confirmation",
            agent: "claude",
            diff: "--- a/.mcp.json\n+++ b/.mcp.json\n@@\n-  \"command\": \"whygraph-mcp\"\n+  \"url\": \"http://127.0.0.1:${WHYGRAPH_PORT:-8765}/mcp/x\"",
          }),
        ])}
        selectedAgents={["claude"]}
        confirmed={new Set()}
        onConfirmChange={onConfirmChange}
      />,
    );
    const row = screen.getByTestId("file-.mcp.json");
    expect(within(row).getByText("Committed file")).toBeInTheDocument();
    expect(within(row).getByTestId("diff")).toHaveTextContent('"command": "whygraph-mcp"');
    const box = within(row).getByRole("checkbox");
    expect(box).not.toBeChecked();
    await user.click(box);
    expect(onConfirmChange).toHaveBeenCalledWith(".mcp.json", true);
  });

  it("reflects a confirmed file and lists the per-agent notes", () => {
    render(
      <InitPreview
        result={result([file({ file: ".mcp.json", status: "needs_confirmation", agent: "claude", diff: "+x" })])}
        selectedAgents={["claude", "vscode", "codex"]}
        confirmed={new Set([".mcp.json"])}
        onConfirmChange={noop}
      />,
    );
    expect(within(screen.getByTestId("file-.mcp.json")).getByRole("checkbox")).toBeChecked();
    const notes = screen.getByTestId("init-preview").querySelector("ul:last-child")!;
    expect(notes).toHaveTextContent(/approve the project's MCP server/);
    expect(notes).toHaveTextContent(/shows up twice in VS Code/);
    expect(notes).toHaveTextContent(/trust this project in Codex/);
  });

  it("summarizes bundled asset files", () => {
    render(
      <InitPreview
        result={result(
          [],
          [
            file({ file: ".claude/agents/a.md", status: "write" }),
            file({ file: ".claude/agents/b.md", status: "skip" }),
          ],
        )}
        selectedAgents={["claude"]}
        confirmed={new Set()}
        onConfirmChange={noop}
      />,
    );
    expect(screen.getByText(/1 to create, 1 already there/)).toBeInTheDocument();
  });
});

// ---- detected panel -------------------------------------------------------------------

describe("DetectedPanel", () => {
  const detected: Detected = {
    existing_db: true,
    managed_hooks: ["post-commit", "post-merge"],
    detected_agents: [
      { agent: "claude", file: ".mcp.json", key: "mcpServers.whygraph", shape: "stdio", stale: false, tracked: true },
      { agent: "vscode", file: ".vscode/mcp.json", key: "mcpServers.whygraph", shape: "stdio", stale: true, tracked: false },
    ],
    custom_db_paths: [
      {
        key: "whygraph_db",
        path: "/data/old/whygraph.db",
        exists: true,
        message: "WhyGraph data at /data/old/whygraph.db will not be used; the portal reads .whygraph/whygraph.db.",
      },
      { key: "codegraph_db", path: "/gone/codegraph.db", exists: false, message: "gone" },
    ],
  };

  it("lists the existing db, the hooks, each agent entry and the custom path warning", () => {
    render(<DetectedPanel detected={detected} removed={[]} onActionChange={() => {}} />);
    const panel = screen.getByTestId("detected-panel");
    expect(panel).toHaveTextContent("Existing database");
    expect(panel).toHaveTextContent("post-commit, post-merge");
    expect(panel).toHaveTextContent("Claude Code entry");
    expect(panel).toHaveTextContent("committed to git");
    expect(panel).toHaveTextContent("VS Code never read this one");
    // Only a custom DB that still exists is warned about, with its path.
    const warnings = screen.getAllByTestId("custom-db-warning");
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toHaveTextContent("/data/old/whygraph.db");
    expect(warnings[0]).toHaveTextContent(/will not be used/);
  });

  it("offers migrate / remove per entry and reports the choice", async () => {
    const onActionChange = vi.fn();
    const user = userEvent.setup();
    render(<DetectedPanel detected={detected} removed={["vscode"]} onActionChange={onActionChange} />);
    const claude = screen.getByRole("group", { name: "Claude Code entry" });
    const vscode = screen.getByRole("group", { name: "VS Code / Copilot entry" });
    expect(within(claude).getByRole("button", { name: "Migrate to HTTP" })).toHaveAttribute("aria-pressed", "true");
    expect(within(vscode).getByRole("button", { name: "Remove entry" })).toHaveAttribute("aria-pressed", "true");

    await user.click(within(claude).getByRole("button", { name: "Remove entry" }));
    expect(onActionChange).toHaveBeenCalledWith("claude", "remove");
    await user.click(within(vscode).getByRole("button", { name: "Migrate to HTTP" }));
    expect(onActionChange).toHaveBeenCalledWith("vscode", "migrate");
  });

  it("renders nothing for a repo with no earlier state", () => {
    const { container } = render(
      <DetectedPanel
        detected={{ existing_db: false, managed_hooks: [], detected_agents: [], custom_db_paths: [] }}
        removed={[]}
        onActionChange={() => {}}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

// ---- not-shared alert -------------------------------------------------------------------

describe("NotSharedAlert", () => {
  it("shows the exact backend command, copies it, and re-checks on demand", async () => {
    const onCheckAgain = vi.fn();
    const user = userEvent.setup();
    const command = "whygraph up --add-folder '/Users/me/My Work'";
    render(<NotSharedAlert command={command} onCheckAgain={onCheckAgain} />);

    expect(screen.getByRole("alert")).toHaveTextContent("This folder isn't shared with the portal");
    expect(screen.getByText(command)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Copy" }));
    expect(await navigator.clipboard.readText()).toBe(command);
    await user.click(screen.getByRole("button", { name: "Check again" }));
    expect(onCheckAgain).toHaveBeenCalledTimes(1);
  });
});

// ---- cost card --------------------------------------------------------------------------

describe("EstimateBody", () => {
  const estimate = {
    commits: 1204,
    upper_bound: true,
    large_commits: 3,
    model: { provider: "anthropic", model: "claude-haiku-4-5" },
    tokens: {
      input: 2_400_000,
      output: 310_000,
      input_range: { low: 1_200_000, high: 3_600_000 },
      output_range: { low: 150_000, high: 450_000 },
    },
    cost: { usd: 4.6, low: 2.3, high: 6.9, currency: "USD", prices_as_of: "2026-09-01" },
    missing_key: null,
  };

  it("shows commits (upper bound), model, tokens and cost, with both choices", async () => {
    const onDescribe = vi.fn();
    const onLater = vi.fn();
    const user = userEvent.setup();
    render(
      <EstimateBody slug="a" estimate={estimate} onDescribe={onDescribe} onLater={onLater} />,
    );
    const card = screen.getByTestId("scan-estimate");
    expect(card).toHaveTextContent("1,204 commits to describe");
    expect(card).toHaveTextContent("(upper bound)");
    expect(card).toHaveTextContent("anthropic/claude-haiku-4-5");
    expect(screen.getByTestId("estimate-tokens")).toHaveTextContent("2.4M input and 310k output tokens");
    expect(card).toHaveTextContent("~$4.60");
    await user.click(screen.getByRole("button", { name: "Describe now" }));
    await user.click(screen.getByRole("button", { name: "Later" }));
    expect(onDescribe).toHaveBeenCalled();
    expect(onLater).toHaveBeenCalled();
  });

  it("shows tokens only for an unpriced model", () => {
    render(
      <EstimateBody slug="a" estimate={{ ...estimate, cost: null }} onDescribe={() => {}} onLater={() => {}} />,
    );
    expect(screen.getByTestId("scan-estimate")).toHaveTextContent("tokens only");
  });

  it("disables Describe now while the provider has no key and links to Configure", async () => {
    await renderWithRouter(
      <EstimateBody
        slug="a"
        estimate={{ ...estimate, missing_key: "anthropic" }}
        onDescribe={() => {}}
        onLater={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Describe now" })).toBeDisabled();
    expect(screen.getByTestId("missing-key")).toHaveTextContent("no key for anthropic");
    expect(screen.getByRole("link", { name: "Add a key in Configure" })).toHaveAttribute(
      "href",
      "/p/a/init?step=configure",
    );
  });
});
