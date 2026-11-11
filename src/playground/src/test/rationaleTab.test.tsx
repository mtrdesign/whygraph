import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRootRoute, createRouter } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RationaleCard } from "../api";
import { RationaleTab } from "../components/RationaleTab";
import { ProjectProvider } from "../lib/project";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const card = (over: Partial<RationaleCard> = {}): RationaleCard => ({
  status: "cached",
  purpose: "Parses the thing",
  why: "Because",
  provider: "anthropic",
  model: "claude-haiku-4-5",
  ...over,
});

let read: RationaleCard;
let posted: RationaleCard;
let posts: number;

beforeEach(() => {
  read = card();
  posted = card();
  posts = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://127.0.0.1:8765");
      if (url.pathname === "/api/projects/alpha/node/rationale") {
        if (init?.method === "POST") {
          posts += 1;
          return Promise.resolve(json(posted));
        }
        return Promise.resolve(json(read));
      }
      if (url.pathname === "/api/projects/alpha")
        return Promise.resolve(
          json({ slug: "alpha", source: "local", initialized: true, permissions: ["project.read", "project.chat"] }),
        );
      return Promise.resolve(json({ error: "unhandled" }, 404));
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

async function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const root = createRootRoute({
    component: () => (
      <QueryClientProvider client={client}>
        <ProjectProvider slug="alpha">
          <RationaleTab qualifiedName="pkg.fn" />
        </ProjectProvider>
      </QueryClientProvider>
    ),
  });
  const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ["/"] }) });
  await act(async () => {
    render(<RouterProvider router={router} />);
  });
  await screen.findByText("Parses the thing");
}

describe("RationaleTab partial card", () => {
  it("shows no note for a complete card, or one without the new fields", async () => {
    await mount();
    expect(screen.queryByTestId("rationale-partial")).toBeNull();
    expect(screen.queryByRole("button", { name: "Complete" })).toBeNull();
  });

  it("shows the note with the count and a Complete button", async () => {
    read = card({ partial: true, describing: 3, completing: false });
    await mount();
    expect(screen.getByTestId("rationale-partial")).toHaveTextContent(
      "Built while 3 commits still had no description",
    );
    expect(screen.getByRole("button", { name: "Complete" })).toBeEnabled();
    expect(screen.queryByTestId("rationale-complete-result")).toBeNull();
  });

  it("Complete with completing true says it runs in the background", async () => {
    read = card({ partial: true, describing: 3 });
    posted = card({ partial: true, describing: 3, completing: true });
    await mount();
    await userEvent.setup().click(screen.getByRole("button", { name: "Complete" }));
    await waitFor(() =>
      expect(screen.getByTestId("rationale-complete-result")).toHaveTextContent(
        "Completing in the background; reopen this tab in a few minutes",
      ),
    );
    expect(posts).toBe(1);
  });

  it("Complete with completing false says to try again later", async () => {
    read = card({ partial: true, describing: 1 });
    posted = card({ partial: true, describing: 1, completing: false });
    await mount();
    await userEvent.setup().click(screen.getByRole("button", { name: "Complete" }));
    await waitFor(() =>
      expect(screen.getByTestId("rationale-complete-result")).toHaveTextContent(
        "Cannot complete it right now; try again later",
      ),
    );
  });
});
