import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { formatBytes } from "../lib/format";
import { monthRange, presetRange } from "../lib/usageRange";
import { STATS_OVERVIEW, fake, json, mountAdmin, resetAdminFake, signedInAdmin, strays } from "./adminHarness";

// The chart card draws to a canvas; these tests cover the page, not ECharts.
vi.mock("echarts-for-react/esm/core", () => ({ default: () => <div data-testid="echart" /> }));
vi.mock("../components/charts/echarts", () => ({ default: {} }));

const SPEND = {
  from: "2026-10-20",
  to: "2026-11-19",
  columns: ["day", "provider", "cost_usd"],
  rows: [
    ["2026-11-17", "openai", 1.5],
    ["2026-11-18", "openai", 2],
  ],
  stack: {
    series: "provider",
    series_index: 1,
    series_values: ["openai"],
    x_values: ["2026-11-17", "2026-11-18"],
    cells: { "2026-11-17": { openai: 1.5 }, "2026-11-18": { openai: 2 } },
    filled_cells: 0,
  },
};
const SCANS = {
  from: "2026-10-20",
  to: "2026-11-19",
  columns: ["day", "status", "runs"],
  rows: [
    ["2026-11-17", "ok", 3],
    ["2026-11-18", "failed", 1],
  ],
  stack: {
    series: "status",
    series_index: 1,
    series_values: ["ok", "failed"],
    x_values: ["2026-11-17", "2026-11-18"],
    cells: { "2026-11-17": { ok: 3, failed: 0 }, "2026-11-18": { ok: 0, failed: 1 } },
    filled_cells: 2,
  },
};
const TOP = {
  from: "2026-10-20",
  to: "2026-11-19",
  metric: "spend",
  items: [
    { slug: "acme", name: "Acme", value: 12.5 },
    { slug: "zeta", name: "Zeta", value: 3 },
  ],
};

function statsRoutes(over: { overview?: unknown; spend?: unknown; scans?: unknown; top?: unknown } = {}) {
  fake.routes["/api/admin/stats/overview"] = () => json(over.overview ?? STATS_OVERVIEW);
  fake.routes["/api/admin/stats/series"] = () => {
    const metric = new URLSearchParams(fake.calls[fake.calls.length - 1].search).get("metric");
    return json((metric === "spend" ? over.spend : over.scans) ?? (metric === "spend" ? SPEND : SCANS));
  };
  fake.routes["/api/admin/stats/top"] = () => json(over.top ?? TOP);
}
const calls = (path: string) => fake.calls.filter((c) => c.path === path).map((c) => new URLSearchParams(c.search));

beforeEach(() => {
  resetAdminFake();
  signedInAdmin();
  statsRoutes();
});
afterEach(() => {
  expect(strays()).toEqual([]);
  vi.unstubAllGlobals();
});

describe("Dashboard", () => {
  it("opens on 30 days and always sends from and to on every stats call", async () => {
    mountAdmin("/admin");
    await screen.findByTestId("tile-users");
    await screen.findByTestId("spend-chart");
    const range = presetRange("30d");
    for (const path of ["/api/admin/stats/overview", "/api/admin/stats/top"]) {
      const [q] = calls(path);
      expect([q.get("from"), q.get("to")]).toEqual([range.from, range.to]);
    }
    const series = calls("/api/admin/stats/series");
    expect(series.map((q) => q.get("metric")).sort()).toEqual(["scans", "spend"]);
    for (const q of series) expect([q.get("from"), q.get("to")]).toEqual([range.from, range.to]);
    expect(calls("/api/admin/stats/top")[0].get("metric")).toBe("spend");
    expect(calls("/api/admin/stats/top")[0].get("limit")).toBe("10");
  });

  it("'This month' sends both bounds, writes them to the URL, and a range change refetches", async () => {
    const router = mountAdmin("/admin");
    await screen.findByTestId("tile-users");
    const u = userEvent.setup();
    await u.click(screen.getByRole("combobox", { name: "Range" }));
    await u.click(await screen.findByRole("option", { name: "This month" }));
    const month = monthRange(0);
    await waitFor(() => expect(router.state.location.search).toEqual({ from: month.from, to: month.to }));
    await waitFor(() => {
      const last = calls("/api/admin/stats/overview").at(-1)!;
      expect([last.get("from"), last.get("to")]).toEqual([month.from, month.to]);
    });
    expect(calls("/api/admin/stats/overview").length).toBeGreaterThan(1);
    // Back to the default range: nothing in the URL.
    await u.click(screen.getByRole("combobox", { name: "Range" }));
    await u.click(await screen.findByRole("option", { name: "Last 30 days" }));
    await waitFor(() => expect(router.state.location.search).toEqual({}));
  });

  it("renders the seven tiles with their details", async () => {
    mountAdmin("/admin");
    await screen.findByTestId("tile-users");
    const tile = (id: string) => screen.getByTestId(id);
    expect(tile("tile-users")).toHaveTextContent("3");
    expect(tile("tile-users")).toHaveTextContent(/\+1 since/);
    expect(tile("tile-orgs")).toHaveTextContent("+0 since");
    expect(tile("tile-projects")).toHaveTextContent("2");
    expect(tile("tile-projects").textContent).not.toMatch(/since|last month|%/);
    expect(tile("tile-spend")).toHaveTextContent("$13");
    expect(tile("tile-spend")).toHaveTextContent("$8.00 last month by this day");
    expect(tile("tile-tokens")).toHaveTextContent("1.2M");
    expect(tile("tile-storage")).toHaveTextContent(formatBytes(1288490189));
    expect(tile("tile-storage")).toHaveTextContent(/\+204\.8 MiB since .*Purged projects are not counted\./);
    expect(tile("tile-agent-calls")).toHaveTextContent("150");
    expect(tile("tile-agent-calls")).toHaveTextContent("+50% on the 7 days before");
  });

  it("shows a minus as a plain hyphen", async () => {
    statsRoutes({
      overview: {
        ...STATS_OVERVIEW,
        kpis: { ...STATS_OVERVIEW.kpis, agent_calls_7d: { value: 50, previous: 100 }, storage_bytes: { value: 1024, previous: 3072 } },
      },
    });
    mountAdmin("/admin");
    await screen.findByTestId("tile-users");
    expect(screen.getByTestId("tile-agent-calls")).toHaveTextContent("-50% on the 7 days before");
    expect(screen.getByTestId("tile-storage")).toHaveTextContent("-2.0 KiB since");
  });

  it("says nothing needs attention when the list is empty", async () => {
    mountAdmin("/admin");
    expect(await screen.findByText("Nothing needs your attention.")).toBeInTheDocument();
  });

  it("words each attention item and links it to its item", async () => {
    const at = new Date(Date.now() - 3 * 3600_000).toISOString();
    statsRoutes({
      overview: {
        ...STATS_OVERVIEW,
        attention: [
          { kind: "scan_failed", org: { slug: "acme", name: "Acme" }, project: { org: "acme", slug: "api", name: "Api" }, at, text: "failed" },
          { kind: "access_lost", org: { slug: "acme", name: "Acme" }, project: { org: "acme", slug: "web", name: "Web" }, at, text: "revoked" },
          { kind: "budget_stopped", org: { slug: "acme", name: "Acme" }, text: "org" },
          { kind: "base_check", text: "*.whygraph.localhost does not resolve" },
          { kind: "update", text: "2.7.0", url: "https://example.com/notes" },
          { kind: "password_owner", user: { uid: "u3", label: "Root" }, count: 2 },
          { kind: "password_not_admin", user: { uid: "u9", label: "Pat" } },
          { kind: "org_suspended", org: { slug: "acme", name: "Acme" }, at, text: "abuse" },
          { kind: "deleted_with_data", org: { slug: "acme", name: "Acme" }, project: { org: "acme", slug: "old", name: "Old" }, at, bytes: 1048576 },
          { kind: "deleted_with_data", org: { slug: "gone", name: "Gone" }, at, bytes: 2048 },
        ],
      },
    });
    mountAdmin("/admin");
    const list = await screen.findByTestId("attention");
    const line = (kind: string, n = 0) => within(list).getAllByTestId(`attention-${kind}`)[n];
    expect(line("scan_failed")).toHaveTextContent("Api in Acme: scan failed 3 h ago");
    expect(within(line("scan_failed")).getByRole("link")).toHaveAttribute("href", "/admin/projects/acme/api");
    expect(line("access_lost")).toHaveTextContent("Web in Acme: GitHub access lost");
    expect(within(line("access_lost")).getByRole("link")).toHaveAttribute("href", "/admin/projects/acme/web");
    expect(line("budget_stopped")).toHaveTextContent("Acme: monthly budget reached");
    expect(within(line("budget_stopped")).getByRole("link")).toHaveAttribute("href", "/admin/orgs/acme?tab=usage");
    expect(line("base_check")).toHaveTextContent("*.whygraph.localhost does not resolve");
    expect(within(line("base_check")).getByRole("link")).toHaveAttribute("href", "/admin/config");
    expect(line("update")).toHaveTextContent("WhyGraph 2.7.0 is available");
    expect(within(line("update")).getByRole("link")).toHaveAttribute("href", "https://example.com/notes");
    expect(line("password_owner")).toHaveTextContent("Root (password account) owns 2 organizations: hand them to a GitHub user");
    expect(within(line("password_owner")).getByRole("link")).toHaveAttribute("href", "/admin/users/u3");
    expect(line("password_not_admin")).toHaveTextContent("Pat can't sign in (password account, not an administrator)");
    expect(within(line("password_not_admin")).getByRole("link")).toHaveAttribute("href", "/admin/users/u9");
    expect(line("org_suspended")).toHaveTextContent("Acme is suspended");
    expect(within(line("org_suspended")).getByRole("link")).toHaveAttribute("href", "/admin/orgs/acme");
    expect(line("deleted_with_data", 0)).toHaveTextContent("Old in Acme is deleted and still holds 1.0 MiB");
    expect(within(line("deleted_with_data", 0)).getByRole("link")).toHaveAttribute("href", "/admin/projects/acme/old");
    expect(line("deleted_with_data", 1)).toHaveTextContent("Gone is deleted and still holds 2.0 KiB");
    expect(within(line("deleted_with_data", 1)).getByRole("link")).toHaveAttribute("href", "/admin/orgs/gone");
    expect(screen.queryByText("Nothing needs your attention.")).toBeNull();
  });

  it("shows attention text from the server as text, never markup", async () => {
    statsRoutes({ overview: { ...STATS_OVERVIEW, attention: [{ kind: "base_check", text: "<img src=x onerror=alert(1)>" }] } });
    mountAdmin("/admin");
    const line = await screen.findByTestId("attention-base_check");
    expect(line).toHaveTextContent("<img src=x onerror=alert(1)>");
    expect(line.querySelector("img")).toBeNull();
  });

  it("an overview failure replaces the attention list and the tiles with one block while the charts render", async () => {
    fake.routes["/api/admin/stats/overview"] = () => json({ error: "boom" }, 500);
    mountAdmin("/admin");
    expect(await screen.findByText("Couldn't load the platform figures")).toBeInTheDocument();
    expect(screen.queryByTestId("admin-tiles")).toBeNull();
    expect(screen.queryByTestId("attention")).toBeNull();
    expect(screen.queryByTestId("attention-none")).toBeNull();
    expect(await screen.findByTestId("spend-chart")).toBeInTheDocument();
    expect(await screen.findByTestId("scans-chart")).toBeInTheDocument();
    expect(await screen.findByTestId("top-chart")).toBeInTheDocument();
  });

  it("a 503 on one chart shows an error in that chart only", async () => {
    fake.routes["/api/admin/stats/top"] = () => json({ error: "slow", code: "usage_timeout" }, 503);
    mountAdmin("/admin");
    const section = await screen.findByTestId("top-section");
    expect(await within(section).findByText("Couldn't load the ranking")).toBeInTheDocument();
    expect(within(section).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(await screen.findByTestId("tile-users")).toBeInTheDocument();
    expect(await screen.findByTestId("spend-chart")).toBeInTheDocument();
    expect(await screen.findByTestId("scans-chart")).toBeInTheDocument();
  });

  it("each empty chart draws its own sentence", async () => {
    const empty = { from: "", to: "", columns: [], rows: [], stack: null };
    statsRoutes({ spend: empty, scans: empty, top: { ...TOP, items: [] } });
    mountAdmin("/admin");
    expect(await screen.findByText("No LLM spend in this range.")).toBeInTheDocument();
    expect(await screen.findByText("No scans in this range.")).toBeInTheDocument();
    expect(await screen.findByText("Nothing to rank in this range.")).toBeInTheDocument();
  });

  it("the top-orgs metric is a native select kept in the URL, and bytes read as GiB in the table", async () => {
    const router = mountAdmin("/admin");
    const select = await screen.findByLabelText("Rank by");
    expect(select.tagName).toBe("SELECT");
    expect(screen.getByRole("heading", { name: "Top 10 organizations by spend" })).toBeInTheDocument();
    statsRoutes({
      top: { ...TOP, metric: "storage", items: [{ slug: "acme", name: "Acme", value: 1288490189 }, { slug: "zeta", name: "Zeta", value: 524288 }] },
    });
    await userEvent.setup().selectOptions(select, "Storage");
    await waitFor(() => expect(router.state.location.search).toEqual({ rank: "storage" }));
    expect(await screen.findByRole("heading", { name: "Top 10 organizations by storage" })).toBeInTheDocument();
    await waitFor(() => expect(calls("/api/admin/stats/top").at(-1)!.get("metric")).toBe("storage"));
    const section = screen.getByTestId("top-section");
    await userEvent.setup().click(await within(section).findByRole("button", { name: "table" }));
    expect(within(section).getByText("1.2 GiB")).toBeInTheDocument();
    expect(within(section).getByText("512.0 KiB")).toBeInTheDocument();
  });

  it("an invalid rank or a half range falls back to the defaults", async () => {
    mountAdmin("/admin?rank=nope&from=2026-11-01");
    await screen.findByTestId("tile-users");
    expect(calls("/api/admin/stats/top")[0].get("metric")).toBe("spend");
    const range = presetRange("30d");
    expect(calls("/api/admin/stats/overview")[0].get("from")).toBe(range.from);
  });

  it("no longer carries the settings check or the updates card (they moved to Configuration)", async () => {
    mountAdmin("/admin");
    expect(await screen.findByText("Nothing needs your attention.")).toBeInTheDocument();
    expect(screen.queryByTestId("base-check-ok")).toBeNull();
    expect(screen.queryByTestId("admin-updates")).toBeNull();
    expect(fake.calls.map((c) => c.path)).not.toContain("/api/admin/config");
  });
});

describe("formatBytes", () => {
  it("uses binary units with one decimal", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(512 * 1024)).toBe("512.0 KiB");
    expect(formatBytes(1288490189)).toBe("1.2 GiB");
    expect(formatBytes(null)).toBe("-");
  });
});
