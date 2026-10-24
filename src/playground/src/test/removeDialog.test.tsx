import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ProjectDetails } from "../api";
import { RemoveProjectDialog } from "../components/portal/RemoveProjectDialog";
import { auditDetails } from "../lib/auditEvents";

vi.mock("@tanstack/react-router", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-router")>()),
  useNavigate: () => vi.fn(() => Promise.resolve()),
}));

const json = (b: unknown) => new Response(JSON.stringify(b), { headers: { "content-type": "application/json" } });

const project = (over: Partial<ProjectDetails> = {}) =>
  ({ slug: "alpha", name: "Alpha", source: "local", agents: [], running_scan: null, root: "/r/alpha", ...over }) as ProjectDetails;

function mount(p: ProjectDetails, state: Promise<Response> | Response) {
  const bodies: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://127.0.0.1:8765");
      if (url.pathname === "/api/portal/state") return Promise.resolve(state);
      if (init?.method === "DELETE") {
        bodies.push(JSON.parse(String(init.body)));
        return Promise.resolve(
          json({ removed: p.slug, hooks: null, agent_files: [], checkout_deleted: false, history_deleted: false, warnings: [] }),
        );
      }
      return Promise.resolve(json({}));
    }),
  );
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RemoveProjectDialog project={p} open onOpenChange={() => {}} />
    </QueryClientProvider>,
  );
  return bodies;
}

const LOCAL = { mode: "local", setup_complete: true, user: { uid: "u", display_name: "A", role: "owner" } };
const PROD = { mode: "production", host_kind: "org", org: { slug: "acme", name: "Acme", role: "owner" } };

afterEach(() => vi.unstubAllGlobals());

describe("RemoveProjectDialog history option", () => {
  it("local wording: Also delete history, unticked, no typed name until ticked", async () => {
    const user = userEvent.setup();
    mount(project(), json(LOCAL));
    const box = await screen.findByRole("checkbox", { name: /Also delete history/ });
    expect(box).not.toBeChecked();
    expect(screen.queryByRole("checkbox", { name: /Keep history/ })).toBeNull();
    expect(screen.getByTestId("remove-dialog")).toHaveTextContent(/commit descriptions, rationale cards and chats/i);
    expect(screen.queryByLabelText(/Type/)).toBeNull();
    await user.click(box);
    expect(screen.getByLabelText(/to confirm deleting the history/)).toBeInTheDocument();
  });

  it("production wording: Keep history, unticked", async () => {
    mount(project({ source: "github" }), json(PROD));
    const box = await screen.findByRole("checkbox", { name: /Keep history/ });
    expect(box).not.toBeChecked();
    expect(screen.getByTestId("remove-dialog")).toHaveTextContent(/add the repository again to get it back/i);
    expect(screen.queryByRole("checkbox", { name: /Also delete history/ })).toBeNull();
  });

  it("is hidden for a linked project", async () => {
    mount(project({ source: "platform" }), json(LOCAL));
    await screen.findByTestId("remove-dialog");
    expect(screen.queryByRole("checkbox", { name: /history/i })).toBeNull();
  });

  it("keeps Remove disabled until the portal state has loaded", async () => {
    let release!: (r: Response) => void;
    const bodies = mount(project(), new Promise<Response>((res) => (release = res)));
    const remove = await screen.findByRole("button", { name: "Remove project" });
    expect(remove).toBeDisabled();
    release(json(LOCAL));
    await waitFor(() => expect(remove).toBeEnabled());
    await userEvent.setup().click(remove);
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ delete_history: false });
  });
});

describe("audit labels", () => {
  it("labels history_deleted", () => {
    const row = { id: 1, created_at: "", org: null, actor: null, event: "project_removed", target: "a", ip: null, fields: { history_deleted: true } };
    expect(auditDetails(row).map((d) => d.label)).toContain("History deleted");
  });
});
