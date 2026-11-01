import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRootRoute, createRouter } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ImportResult, Note, TransferStatus } from "../api";
import { ImportedBadge, NoteBadges } from "../components/notes/NoteCard";
import { ImportPreviewDialog } from "../components/settings/ImportPreviewDialog";
import { SettingsLayout } from "../components/settings/SettingsLayout";
import { TransferSection } from "../components/settings/TransferSection";
import { errorMessage } from "../lib/apiErrors";
import { ApiError } from "../api";

vi.mock("@tanstack/react-router", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-router")>()),
  useNavigate: () => vi.fn(),
}));

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const result = (over: Partial<ImportResult> = {}): ImportResult => ({
  applied: false,
  source: { mode: "production", label: "acme/web", whygraph_version: "2.2.1", exported_at: "2026-10-30T10:00:00Z" },
  repo: { matched_roots: 1, source_origin: "github.com/acme/web", target_origin: "github.com/acme/web" },
  notes: {
    inserted: { proposed: 1, confirmed: 2, retired: 0 },
    skipped_duplicate: 0,
    skipped_purpose: 0,
    skipped_oversize: 0,
    skipped_denied: 0,
    refs_unresolved: 0,
    revision_unlinked: 0,
    downgraded: 0,
  },
  wide_confirmed: [{ kind: "purpose", anchor: "project", body: "Always run the migration first.\nThen restart." }],
  purpose_runs: { inserted: 0 },
  rationale: { inserted: 5, existing: 0, inactive_model: 2, skipped_oversize: 0 },
  descriptions: { filled: 10, kept_target: 0, missing_commit: 0, skipped_oversize: 0 },
  chats: { sessions: 3, matched_existing: 0, skipped_oversize: 0, messages: 40, providers: ["anthropic"] },
  config: { present: true, invalid: [], changes: [{ key: "rationale.model", from: null, to: "claude-x" }], applied: false, error: null },
  backup: { written: false, skipped_low_disk: false, path: null },
  ...over,
});

function dialog(preview: ImportResult, onImport = vi.fn(), res: ImportResult | null = null) {
  render(
    <ImportPreviewDialog open onOpenChange={() => {}} preview={preview} result={res} pending={false} error={null} onImport={onImport} />,
  );
  return onImport;
}

describe("ImportPreviewDialog", () => {
  it("shows counts, wide notes verbatim and the chat ownership line", () => {
    dialog(result());
    expect(screen.getByTestId("import-counts")).toHaveTextContent("2 confirmed notes");
    expect(screen.getByTestId("import-counts")).toHaveTextContent("10 commit or file descriptions");
    const note = screen.getByTestId("import-wide-note");
    expect(note.textContent).toContain("Always run the migration first.\nThen restart.");
    expect(screen.getByText(/will reach every agent session/)).toBeInTheDocument();
    expect(screen.getByTestId("import-chats")).toHaveTextContent("3 chats");
    expect(screen.getByTestId("import-chats")).toHaveTextContent("your");
    expect(screen.getByTestId("import-inactive")).toBeInTheDocument();
  });

  it("apply settings is off by default and is sent only when ticked", async () => {
    const user = userEvent.setup();
    const onImport = dialog(result());
    const box = screen.getByRole("checkbox", { name: /Apply these settings/ });
    expect(box).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "Import" }));
    expect(onImport).toHaveBeenLastCalledWith(false);
    await user.click(box);
    await user.click(screen.getByRole("button", { name: "Import" }));
    expect(onImport).toHaveBeenLastCalledWith(true);
  });

  it("disables the settings box when the config is invalid", () => {
    dialog(result({ config: { present: true, invalid: ["foo.bar"], changes: [{ key: "a", from: 1, to: 2 }], applied: false, error: null } }));
    expect(screen.getByRole("checkbox", { name: /Apply these settings/ })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByTestId("import-config-invalid")).toHaveTextContent("foo.bar");
  });

  it("says when a cache member was only partly imported", () => {
    dialog(result());
    expect(screen.queryByTestId("import-partial")).toBeNull();
    const applied = result({
      applied: true,
      descriptions: { filled: 4, kept_target: 0, missing_commit: 0, skipped_oversize: 0, partial: true, error: "stopped" },
    });
    dialog(result(), vi.fn(), applied);
    expect(screen.getByTestId("import-partial")).toHaveTextContent("Partially imported: not all descriptions landed");
  });

  it("shows the backup path after an apply", () => {
    const applied = result({ applied: true, backup: { written: true, skipped_low_disk: false, path: "/data/backups/x.db" } });
    dialog(result(), vi.fn(), applied);
    expect(screen.getByText("Bundle imported")).toBeInTheDocument();
    expect(screen.getByTestId("import-backup")).toHaveTextContent("/data/backups/x.db");
    expect(screen.queryByRole("button", { name: "Import" })).toBeNull();
  });
});

describe("transfer error mapping", () => {
  it("words the new codes through the registry", () => {
    expect(errorMessage(new ApiError(415, "x", "bundle_invalid"))).toMatch(/isn't a WhyGraph bundle/);
    expect(errorMessage(new ApiError(409, "x", "bundle_wrong_repo"))).toMatch(/another repository/);
    expect(errorMessage(new ApiError(409, "x", "transfer_needs_scan"))).toMatch(/first scan/);
  });
});

describe("imported badge", () => {
  it("renders", () => {
    render(<ImportedBadge />);
    expect(screen.getByTestId("imported-badge")).toHaveTextContent("imported");
  });
  it("NoteBadges still renders", () => {
    render(<NoteBadges note={{ kind: "decision", status: "confirmed", anchor_state: "n/a" } as Note} />);
    expect(screen.queryByTestId("imported-badge")).toBeNull();
  });
});

describe("TransferSection", () => {
  let status: TransferStatus;
  let calls: { path: string; method: string; headers: Record<string, string>; body: unknown }[];
  beforeEach(() => {
    status = { scanned: true, imported_rationale: 0, max_bundle_bytes: 1000, imports: [] };
    calls = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://127.0.0.1:8765");
        calls.push({ path: url.pathname + url.search, method: init?.method ?? "GET", headers: (init?.headers ?? {}) as Record<string, string>, body: init?.body });
        if (url.pathname === "/api/projects/alpha/transfer") return Promise.resolve(json(status));
        if (url.pathname.endsWith("/transfer/import")) return Promise.resolve(json(result({ applied: !url.search.includes("dry_run=1") })));
        if (url.pathname.endsWith("/transfer/rationale")) return Promise.resolve(json({ removed: 3 }));
        return Promise.resolve(json({}));
      }),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  async function mount() {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const root = createRootRoute({
      component: () => (
        <QueryClientProvider client={client}>
          <SettingsLayout title="Settings" sections={[{ id: "transfer", label: "Export and import" }]}>
            <TransferSection slug="alpha" />
          </SettingsLayout>
        </QueryClientProvider>
      ),
    });
    const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ["/"] }) });
    await act(async () => {
      render(<RouterProvider router={router} />);
    });
  }

  it("summarises stored import counts, marking a partial one", async () => {
    status.imports = [
      {
        id: 1, imported_at: "2026-10-30T10:00:00Z", imported_by: "u1", imported_by_name: "Ada", source_label: "alpha-laptop", source_mode: "local", whygraph_version: "2.3.0",
        counts: {
          notes: { inserted: { proposed: 1, confirmed: 2, retired: 1 } } as never,
          rationale: { inserted: 5, existing: 0, inactive_model: 0, skipped_oversize: 0, partial: true, error: "x" },
          descriptions: { filled: 7, kept_target: 0, missing_commit: 0, skipped_oversize: 0 },
          chats: { sessions: 2, matched_existing: 0, skipped_oversize: 0, messages: 9, providers: [] },
        },
      },
      { id: 2, imported_at: "2026-10-29T10:00:00Z", imported_by: "u1", imported_by_name: null, source_label: "old", source_mode: "local", whygraph_version: "2.2.0", counts: {} },
    ];
    await mount();
    const hist = await screen.findByTestId("transfer-history");
    expect(hist).toHaveTextContent("4 notes, 5 rationale cards, 7 descriptions, 2 chats (partial)");
    expect(hist).toHaveTextContent("0 notes, 0 rationale cards, 0 descriptions, 0 chats");
  });

  it("dry-runs on choose, then re-sends the same file with the client header", async () => {
    const user = userEvent.setup();
    await mount();
    await screen.findByTestId("transfer-section");
    const file = new File(["zip"], "bundle.zip", { type: "application/zip" });
    await user.upload(screen.getByTestId("import-file"), file);
    await screen.findByTestId("import-dialog");
    const dry = calls.find((c) => c.path.includes("dry_run=1"))!;
    expect(dry.method).toBe("POST");
    expect(dry.headers["X-WhyGraph-Client"]).toBe("1");
    expect(dry.headers["Content-Type"]).toBe("application/zip");
    expect(dry.body).toBe(file);
    await user.click(within(screen.getByTestId("import-dialog")).getByRole("button", { name: "Import" }));
    await waitFor(() => expect(calls.some((c) => c.path.includes("dry_run=0&apply_config=0"))).toBe(true));
    expect(calls.find((c) => c.path.includes("dry_run=0"))!.body).toBe(file);
    await screen.findByText("Bundle imported");
  });

  it("disables export and import before the first scan", async () => {
    status.scanned = false;
    await mount();
    expect(await screen.findByTestId("transfer-blocked")).toHaveTextContent("Run the first scan first");
    expect(screen.getByRole("button", { name: "Export bundle" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Choose a bundle…" })).toBeDisabled();
  });

  it("shows Remove only with imported cards", async () => {
    await mount();
    await screen.findByRole("button", { name: "Export bundle" });
    expect(screen.queryByTestId("transfer-remove")).toBeNull();
  });

  it("removes imported cards behind a confirmation", async () => {
    status.imported_rationale = 3;
    const user = userEvent.setup();
    await mount();
    await user.click(await screen.findByRole("button", { name: /Remove imported rationale cards \(3\)/ }));
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await user.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.path.endsWith("/transfer/rationale"))).toBe(true));
  });

  it("exports with chats off by default", async () => {
    const user = userEvent.setup();
    URL.createObjectURL = vi.fn(() => "blob:x");
    URL.revokeObjectURL = vi.fn();
    vi.mocked(fetch).mockImplementationOnce(() => Promise.resolve(json(status)));
    await mount();
    await screen.findByRole("button", { name: "Export bundle" });
    expect(screen.getByRole("checkbox", { name: /Include my chats/ })).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "Export bundle" }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/transfer/export?chats=0"))).toBe(true));
  });
});
