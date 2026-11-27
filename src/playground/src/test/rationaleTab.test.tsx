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
let postUrls: string[];
let profile: unknown;

beforeEach(() => {
  read = card();
  posted = card();
  posts = 0;
  postUrls = [];
  profile = { status: "computing" };
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://127.0.0.1:8765");
      if (url.pathname === "/api/projects/alpha/node/rationale") {
        if (init?.method === "POST") {
          posts += 1;
          postUrls.push(url.search);
          return Promise.resolve(json(posted));
        }
        return Promise.resolve(json(read));
      }
      if (url.pathname === "/api/projects/alpha/rationale/depth-profile") return Promise.resolve(json(profile));
      if (url.pathname === "/api/projects/alpha")
        return Promise.resolve(
          json({ slug: "alpha", source: "local", initialized: true, permissions: ["project.read", "project.chat"] }),
        );
      return Promise.resolve(json({ error: "unhandled" }, 404));
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

async function mountRaw() {
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
}

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

describe("RationaleTab item sources", () => {
  const item = (over: Partial<NonNullable<RationaleCard["constraints"]>[number]>) => ({
    text: "t",
    basis: "unknown" as const,
    refs: [],
    ...over,
  });

  it("tags each basis, and nothing for unknown", async () => {
    read = card({
      constraints: [
        item({ text: "one", basis: "quoted", quote: "the words" }),
        item({ text: "two", basis: "cited" }),
        item({ text: "three", basis: "inferred" }),
        item({ text: "four", basis: "unknown" }),
      ],
    });
    await mount();
    const tags = screen.getAllByTestId("rationale-basis").map((e) => e.textContent);
    expect(tags).toEqual(["quoted", "cited", "inferred"]);
    expect(screen.getByText("quoted")).toHaveAttribute("title", "the words");
    expect(screen.getByText("four")).toBeInTheDocument();
  });

  it("renders refs: PR link from its url, commit as 8 characters", async () => {
    read = card({
      risks: [
        item({
          text: "risky",
          basis: "cited",
          refs: [
            { kind: "pr", id: "12", url: "https://github.com/o/r/pull/12" },
            { kind: "commit", id: "0123456789abcdef0123456789abcdef01234567" },
            { kind: "issue", id: "7" },
          ],
        }),
      ],
    });
    await mount();
    expect(screen.getByRole("link", { name: "PR #12" })).toHaveAttribute("href", "https://github.com/o/r/pull/12");
    expect(screen.getByText("01234567")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "01234567" })).toBeNull();
    expect(screen.getByText("issue #7")).toBeInTheDocument();
  });
});

describe("RationaleTab related plans", () => {
  const plans = [
    {
      stem: "p-one",
      title: "Plan one",
      status: "shipped" as const,
      uri: "whygraph://plan/p-one",
      link: "commit" as const,
      hints: [
        { id: "step 4", title: "Notes page: add the Kind dropdown", n: 8 },
        { id: "Q1", title: "What should the dropdown filter by?", n: 12 },
        { id: "extra", title: "Third", n: 13 },
      ],
    },
    { stem: "p-two", title: "Plan two", status: "in_flight" as const, uri: "u", link: "pr" as const, present: false },
    { stem: "p-three", title: "Plan three", status: "not_implemented" as const, uri: "u", link: "declared" as const },
  ];

  it("lists plans under a cached card with phrases, hints (at most 2) and the intent line", async () => {
    read = card({ plans });
    await mount();
    const block = await screen.findByTestId("related-plans");
    expect(block).toHaveTextContent("Related plans");
    expect(screen.getByRole("link", { name: "Plan one" })).toHaveAttribute("href", "/p/alpha/plans?stem=p-one");
    expect(block).toHaveTextContent("built by commits that name this plan");
    expect(block).toHaveTextContent("built in a PR that names this plan");
    expect(block).toHaveTextContent("this plan declares a change to this code");
    expect(block).toHaveTextContent("No longer in the repository");
    expect(screen.getByRole("link", { name: "step 4: Notes page: add the Kind dropdown" })).toHaveAttribute(
      "href",
      "/p/alpha/plans?stem=p-one&section=8",
    );
    expect(screen.getByRole("link", { name: "Q1: What should the dropdown filter by?" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Third/ })).toBeNull();
    expect(block).toHaveTextContent("See:");
    expect(block).toHaveTextContent("Plans state what was intended, not what was built.");
    // A plan without hints shows title, status and link kind only.
    const three = screen.getByTestId("related-plan-p-three");
    expect(three).not.toHaveTextContent("See:");
    expect(three).toHaveTextContent("Not implemented");
  });

  it("shows under the not-generated and no-history messages", async () => {
    read = { status: "not_generated", plans } as RationaleCard;
    await act(async () => {
      await mountRaw();
    });
    expect(await screen.findByTestId("related-plans")).toBeInTheDocument();
  });

  it("shows under no history", async () => {
    read = { status: "no_evidence", plans } as RationaleCard;
    await mountRaw();
    expect(await screen.findByTestId("related-plans")).toBeInTheDocument();
    expect(screen.getByTestId("rationale-no-evidence")).toBeInTheDocument();
  });

  it("shows the plans a Generate answer carries", async () => {
    read = { status: "not_generated" } as RationaleCard;
    posted = card({ plans });
    const user = userEvent.setup();
    await mountRaw();
    expect(screen.queryByTestId("related-plans")).toBeNull();
    await user.click(await screen.findByRole("button", { name: "Generate rationale" }));
    expect(await screen.findByTestId("related-plans")).toBeInTheDocument();
  });

  it("renders nothing for an empty or absent list", async () => {
    read = card({ plans: [] });
    await mount();
    expect(screen.queryByTestId("related-plans")).toBeNull();
  });
});

const level = (cost: { low: number; high: number } | null) => ({
  calls: 1,
  input_tokens: { p50: 10000, p90: 20000 },
  output_tokens: 1500,
  cost,
  first_card: null,
});

const readyProfile = (medium: { low: number; high: number } | null) => ({
  status: "ready",
  head: "abc",
  computed_at: "2026-11-27T10:00:00+00:00",
  sampled: 20,
  model: { provider: "anthropic", model: "claude-sonnet-5-5", source: "pinned", price_known: medium !== null },
  prices_as_of: "2026-11-11",
  output_source: "default",
  cost_hidden: false,
  levels: { low: level(null), medium: level(medium) },
  descriptions: { undescribed_share: 0, per_commit_usd: null },
  cheaper_descriptions: null,
});

describe("RationaleTab depth", () => {
  const built = (over: Partial<RationaleCard> = {}) =>
    card({
      depth: { requested: "auto", resolved: "low", reason: 'Auto chose Low because the task was "explain".' },
      deeper: { level: "medium", adds: "Reads 4 of the 9 other commits to this file." },
      coverage: { line_commits: 2, context_commits: 4, context_omitted: 5 },
      ...over,
    });

  it("shows the level badge, the reason and what the card read", async () => {
    read = built();
    await mount();
    expect(screen.getByTestId("depth-badge")).toHaveTextContent("Low - Lines");
    const notes = screen.getByTestId("rationale-built");
    expect(notes).toHaveTextContent('Auto chose Low because the task was "explain".');
    expect(notes).toHaveTextContent("Read 2 commits that wrote these lines, 4 of 9 other commits to the file.");
    expect(notes).toHaveTextContent("A Medium card reads 4 of the 9 other commits to this file.");
  });

  it("says what the card left out, and that newer commits are not reflected", async () => {
    read = built({
      depth: { requested: "medium", resolved: "medium", reason: "" },
      deeper: null,
      // A card stored before the verification pass was removed (R49): its key is ignored.
      coverage: { line_commits: 1, verification: "failed", descriptions_missing: 2 } as RationaleCard["coverage"],
      context_stale: true,
    });
    await mount();
    const notes = screen.getByTestId("rationale-built");
    expect(notes).toHaveTextContent("Built at Medium.");
    expect(notes).not.toHaveTextContent(/verification/i);
    expect(notes).toHaveTextContent("Newer commits to this file are not reflected.");
    expect(notes).toHaveTextContent("2 commits had no description when this card was built.");
  });

  it("Generate deeper posts the next level and shows the price when it is known", async () => {
    read = built();
    posted = built({ depth: { requested: "medium", resolved: "medium", reason: "" }, deeper: null });
    profile = readyProfile({ low: 0.03, high: 0.05 });
    await mount();
    const button = await screen.findByRole("button", { name: "Generate deeper - Medium · about $0.03-0.05" });
    await userEvent.setup().click(button);
    await waitFor(() => expect(posts).toBe(1));
    expect(postUrls[0]).toContain("depth=medium");
    // The answer replaces the card: now at Medium, the highest offered level, with nothing deeper to offer.
    await waitFor(() => expect(screen.getByTestId("depth-badge")).toHaveTextContent("Medium - Context"));
    expect(screen.queryByTestId("generate-deeper")).toBeNull();
  });

  it("shows no price while it is unknown", async () => {
    read = built();
    profile = readyProfile(null);
    await mount();
    expect(await screen.findByRole("button", { name: "Generate deeper - Medium" })).toBeEnabled();
  });

  it("never offers a level that is not offered, whatever the card says", async () => {
    read = built({
      depth: { requested: "medium", resolved: "medium", reason: "" },
      deeper: { level: "high", adds: "Reads more." },
    });
    await mount();
    expect(screen.getByTestId("depth-badge")).toHaveTextContent("Medium - Context");
    expect(screen.queryByTestId("generate-deeper")).toBeNull();
  });

  it("offers no Generate deeper at the ceiling", async () => {
    read = built({ deeper: null });
    await mount();
    expect(screen.queryByTestId("generate-deeper")).toBeNull();
  });
});
