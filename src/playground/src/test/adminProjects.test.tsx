import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ORGS, ORG_DETAIL, PROJECT_DETAIL, fake, json, mountAdmin, resetAdminFake, signedInAdmin, strays } from "./adminHarness";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }));
// The chart card draws to a canvas; these tests cover the page, not ECharts.
vi.mock("echarts-for-react/esm/core", () => ({ default: () => <div data-testid="echart" /> }));
vi.mock("../components/charts/echarts", () => ({ default: {} }));

let detail: Record<string, unknown>;

beforeEach(() => {
  resetAdminFake();
  signedInAdmin();
  vi.mocked(toast.success).mockClear();
  vi.mocked(toast.error).mockClear();
  vi.mocked(toast.warning).mockClear();
  detail = { ...PROJECT_DETAIL };
  fake.routes["/api/admin/projects/acme/api"] = () => json(detail);
});
afterEach(() => {
  expect(strays()).toEqual([]);
  vi.unstubAllGlobals();
});

const rows = () => screen.queryAllByTestId(/^admin-project-/).map((r) => r.getAttribute("data-testid"));

describe("Projects list", () => {
  it("All holds no deleted row; Deleted holds only deleted ones, with the badge", async () => {
    mountAdmin("/admin/projects");
    await screen.findByTestId("admin-project-acme-api");
    expect(rows()).toEqual(["admin-project-acme-api", "admin-project-zeta-web"]);
    const row = screen.getByTestId("admin-project-acme-api");
    expect(within(row).getByText("acme/api")).toBeInTheDocument();
    expect(within(row).getByText("1.2 GiB")).toBeInTheDocument();
    expect(within(row).getByText("$0.75")).toBeInTheDocument();
    expect(within(screen.getByTestId("admin-project-zeta-web")).getByText("Failing")).toBeInTheDocument();
    await userEvent.setup().selectOptions(screen.getByLabelText("Show"), "Deleted");
    await waitFor(() => expect(rows()).toEqual(["admin-project-acme-old"]));
    expect(within(screen.getByTestId("admin-project-acme-old")).getByText("Deleted")).toBeInTheDocument();
  });

  it("filters by organization, state and search, all kept in the URL; a default stays out", async () => {
    const router = mountAdmin("/admin/projects?state=nope");
    const u = userEvent.setup();
    await screen.findByTestId("admin-project-acme-api");
    expect(rows()).toHaveLength(2);
    await u.selectOptions(screen.getByLabelText("Organization"), "zeta");
    await waitFor(() => expect(rows()).toEqual(["admin-project-zeta-web"]));
    expect(router.state.location.search).toEqual({ org: "zeta" });
    await u.selectOptions(screen.getByLabelText("Organization"), "");
    await u.selectOptions(screen.getByLabelText("Show"), "Failing");
    await waitFor(() => expect(rows()).toEqual(["admin-project-zeta-web"]));
    expect(router.state.location.search).toEqual({ state: "failing" });
    await u.selectOptions(screen.getByLabelText("Show"), "");
    await u.type(screen.getByLabelText("Search"), "api");
    await waitFor(() => expect(rows()).toEqual(["admin-project-acme-api"]));
    expect(router.state.location.search).toEqual({ q: "api" });
  });

  it("opens the detail page from a row", async () => {
    const router = mountAdmin("/admin/projects");
    await userEvent.setup().click(await screen.findByTestId("admin-project-acme-api"));
    await waitFor(() => expect(router.state.location.pathname).toBe("/admin/projects/acme/api"));
  });

  it("Cmd-K finds a project by name, slug or org and opens it", async () => {
    const router = mountAdmin("/admin");
    const user = userEvent.setup();
    await screen.findByRole("heading", { name: "Dashboard" });
    await user.keyboard("{Meta>}k{/Meta}");
    await user.type(await screen.findByPlaceholderText("Go to a page, user, organization or project..."), "old");
    const dialog = screen.getByRole("dialog");
    expect(await within(dialog).findByText("acme/old")).toBeInTheDocument();
    expect(within(dialog).getByText("Deleted")).toBeInTheDocument();
    await user.click(within(dialog).getByText("acme/old"));
    await waitFor(() => expect(router.state.location.pathname).toBe("/admin/projects/acme/old"));
  });
});

describe("Storage columns", () => {
  it("the Organizations list shows an org's storage, and '-' without a sample", async () => {
    fake.routes["/api/admin/orgs"] = () =>
      json([{ ...ORGS[0], storage_bytes: 1288490189 }, { ...ORGS[0], slug: "bare", name: "Bare", storage_bytes: null }]);
    mountAdmin("/admin/orgs");
    const row = await screen.findByTestId("admin-org-acme");
    expect(within(row).getByText("1.2 GiB")).toBeInTheDocument();
    expect(within(screen.getByTestId("admin-org-bare")).getAllByText("-").length).toBeGreaterThan(0);
  });

  it("an org's Projects tab links each project and shows its storage; the Usage tab lists storage per project", async () => {
    fake.routes["/api/admin/orgs/acme"] = () =>
      json({
        ...ORG_DETAIL,
        projects: [{ ...ORG_DETAIL.projects[0], storage_bytes: 1288490189 }, ORG_DETAIL.projects[1]],
      });
    const router = mountAdmin("/admin/orgs/acme?tab=projects");
    const row = await screen.findByTestId("org-project-api");
    expect(within(row).getByText("1.2 GiB")).toBeInTheDocument();
    await userEvent.setup().click(within(row).getByRole("link", { name: /Api/ }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/admin/projects/acme/api"));
  });

  it("the Usage tab shows storage per project", async () => {
    fake.routes["/api/admin/orgs/acme"] = () =>
      json({ ...ORG_DETAIL, projects: [{ ...ORG_DETAIL.projects[0], storage_bytes: 524288 }, ORG_DETAIL.projects[1]] });
    mountAdmin("/admin/orgs/acme?tab=usage");
    const row = await screen.findByTestId("org-storage-api");
    expect(within(row).getByText("512.0 KiB")).toBeInTheDocument();
  });
});

describe("Project detail", () => {
  it("shows the tiles, the scans and the storage charts", async () => {
    mountAdmin("/admin/projects/acme/api");
    await screen.findByRole("heading", { level: 1, name: "Api" });
    expect(screen.getByTestId("tile-project-spend")).toHaveTextContent("$0.75");
    expect(screen.getByTestId("tile-project-spend")).toHaveTextContent("of $5.00");
    expect(screen.getByTestId("tile-project-calls")).toHaveTextContent("42");
    expect(screen.getByTestId("tile-project-scan")).toHaveTextContent("ago");
    expect(screen.getByTestId("project-run-7")).toHaveTextContent("Ben");
    expect(screen.getByRole("heading", { name: "Storage by part" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Total storage, last 90 days" })).toBeInTheDocument();
    expect(screen.getByText("Purged projects are not shown.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rescan" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
  });

  it("shows the last failure's message as text", async () => {
    detail = {
      ...PROJECT_DETAIL,
      overview: { ...PROJECT_DETAIL.overview, last_failure: { run_id: 7, at: "2026-11-17T10:00:00Z", message: "<b>boom</b>" } },
    };
    mountAdmin("/admin/projects/acme/api");
    const alert = await screen.findByTestId("last-failure");
    expect(alert).toHaveTextContent("<b>boom</b>");
    expect(alert.querySelector("b")).toBeNull();
  });

  it("a project that does not exist links back to the projects", async () => {
    fake.routes["/api/admin/projects/acme/nope"] = () => json({ error: "no", code: "not_found" }, 404);
    mountAdmin("/admin/projects/acme/nope");
    expect(await screen.findByTestId("not-found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to projects" })).toHaveAttribute("href", "/admin/projects");
  });

  it("Rescan posts and says 'Rescan queued'; a refusal is a toast", async () => {
    fake.routes["POST /api/admin/projects/acme/api/rescan"] = () => json({ run_id: 8 }, 202);
    mountAdmin("/admin/projects/acme/api");
    const u = userEvent.setup();
    await u.click(await screen.findByRole("button", { name: "Rescan" }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Rescan queued"));
    fake.routes["POST /api/admin/projects/acme/api/rescan"] = () => json({ error: "lost", code: "github_access_lost" }, 409);
    await u.click(screen.getByRole("button", { name: "Rescan" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
  });

  it("Measure now reads 'Measuring...' while it runs, then says 'Storage measured'", async () => {
    let finish: (r: Response) => void = () => {};
    const pending = new Promise<Response>((r) => (finish = r));
    fake.routes["POST /api/admin/projects/acme/api/storage"] = () => pending as unknown as Response;
    mountAdmin("/admin/projects/acme/api");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Measure now" }));
    const button = await screen.findByRole("button", { name: "Measuring..." });
    expect(button).toBeDisabled();
    finish(json({ measured: true, sample: PROJECT_DETAIL.storage.latest }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Storage measured"));
    expect(await screen.findByRole("button", { name: "Measure now" })).toBeEnabled();
  });

  it("Measure now says another measurement is running on 409 busy", async () => {
    fake.routes["POST /api/admin/projects/acme/api/storage"] = () => json({ error: "busy", code: "busy" }, 409);
    mountAdmin("/admin/projects/acme/api");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Measure now" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Another storage measurement is running. Try again in a few minutes."),
    );
  });

  it("a queued or running scan has Cancel and the list refetches while it is active", async () => {
    detail = { ...PROJECT_DETAIL, runs: [{ ...PROJECT_DETAIL.runs[0], id: 9, status: "running", finished_at: null }, PROJECT_DETAIL.runs[0]] };
    fake.routes["POST /api/admin/projects/acme/api/scans/9/cancel"] = () => json({ run_id: 9, was: "running" }, 202);
    mountAdmin("/admin/projects/acme/api");
    const running = await screen.findByTestId("project-run-9");
    expect(within(screen.getByTestId("project-run-7")).queryByRole("button", { name: "Cancel" })).toBeNull();
    await userEvent.setup().click(within(running).getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(fake.calls.some((c) => c.method === "POST" && c.path === "/api/admin/projects/acme/api/scans/9/cancel")).toBe(true),
    );
  });

  it("a log tail containing markup renders as text", async () => {
    fake.routes["/api/admin/projects/acme/api/scans/7/log"] = () =>
      json({ run_id: 7, text: "<img src=x onerror=alert(1)>\nline two", size: 40, truncated: true });
    mountAdmin("/admin/projects/acme/api");
    await userEvent.setup().click(within(await screen.findByTestId("project-run-7")).getByRole("button", { name: "Log" }));
    const pre = await screen.findByTestId("run-log-text");
    expect(pre).toHaveTextContent("<img src=x onerror=alert(1)>");
    expect(pre.querySelector("img")).toBeNull();
    expect(screen.getByTestId("run-log")).toHaveTextContent("Showing the end of the log");
  });

  it("a deleted project shows only Restore and Delete permanently, and no Rescan or Measure now", async () => {
    detail = { ...PROJECT_DETAIL, deleted_at: "2026-11-10T09:00:00Z" };
    mountAdmin("/admin/projects/acme/api");
    expect(await screen.findByTestId("deleted-alert")).toHaveTextContent("Deleted");
    expect(screen.getByRole("button", { name: "Restore" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete permanently" })).toBeInTheDocument();
    for (const name of ["Rescan", "Measure now", "Delete", "Cancel"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
  });

  it("disables Rescan with its reason when the organization is suspended", async () => {
    fake.routes["/api/admin/orgs"] = () => json([{ ...ORGS[0], suspended: { at: "2026-11-01T00:00:00Z", reason: "abuse" } }]);
    mountAdmin("/admin/projects/acme/api");
    await waitFor(() => expect(screen.getByRole("button", { name: "Rescan" })).toBeDisabled());
    expect(screen.getByText("The organization is suspended.")).toBeInTheDocument();
  });
});
