import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ME, ORGS, ORG_DETAIL, USERS, fake, json, mountAdmin, resetAdminFake, signedInAdmin, strays } from "./adminHarness";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }));

const GONE = "2026-11-10T09:00:00Z";
let detail: Record<string, unknown>;

beforeEach(() => {
  resetAdminFake();
  signedInAdmin();
  vi.mocked(toast.error).mockClear();
  detail = { ...ORG_DETAIL };
  fake.routes["/api/admin/orgs/acme"] = () => json(detail);
});
afterEach(() => {
  expect(strays()).toEqual([]);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const rows = () => screen.queryAllByTestId(/^admin-org-/).map((r) => r.getAttribute("data-testid"));
const lists = (extra: Record<string, unknown>[]) => {
  fake.routes["/api/admin/orgs"] = () => json([...ORGS, ...extra]);
};

describe("Organizations list", () => {
  const more = [
    { ...ORGS[0], slug: "susp", name: "Susp", suspended: { at: "2026-11-01T00:00:00Z", reason: "abuse" } },
    { ...ORGS[0], slug: "dead", name: "Dead", deleted_at: GONE, deleted_by: "u1" },
  ];

  it("All holds no deleted row and shows a suspended one with its badge; Deleted holds only deleted ones", async () => {
    lists(more);
    mountAdmin("/admin/orgs");
    await screen.findByTestId("admin-org-acme");
    expect(rows()).toEqual(["admin-org-acme", "admin-org-susp"]);
    expect(within(screen.getByTestId("admin-org-susp")).getByText("Suspended")).toBeInTheDocument();
    await userEvent.setup().selectOptions(screen.getByLabelText("Show"), "Deleted");
    await waitFor(() => expect(rows()).toEqual(["admin-org-dead"]));
    expect(within(screen.getByTestId("admin-org-dead")).getByText("Deleted")).toBeInTheDocument();
  });

  it("Suspended shows suspended ones, in the URL; an invalid status falls back to All and defaults stay out", async () => {
    lists(more);
    const router = mountAdmin("/admin/orgs?status=nope");
    const u = userEvent.setup();
    await screen.findByTestId("admin-org-acme");
    expect(router.state.location.search).toEqual({});
    await u.selectOptions(screen.getByLabelText("Show"), "Suspended");
    await waitFor(() => expect(rows()).toEqual(["admin-org-susp"]));
    expect(router.state.location.search).toEqual({ status: "suspended" });
    await u.type(screen.getByLabelText("Search"), "su");
    await waitFor(() => expect(router.state.location.search).toEqual({ status: "suspended", q: "su" }));
  });

  it("opens an org's detail page from its row", async () => {
    const router = mountAdmin("/admin/orgs");
    await userEvent.setup().click(within(await screen.findByTestId("admin-org-acme")).getAllByRole("link", { name: /Acme/ })[0]);
    await waitFor(() => expect(router.state.location.pathname).toBe("/admin/orgs/acme"));
  });
});

describe("Create organization", () => {
  async function open() {
    fake.routes["/api/admin/users"] = () => json([...USERS, { ...USERS[1], uid: "u9", github_login: "gone", deleted_at: GONE }]);
    mountAdmin("/admin/orgs");
    const u = userEvent.setup();
    await u.click(await screen.findByRole("button", { name: "Create organization" }));
    const dialog = await screen.findByTestId("create-org-dialog");
    return { u, dialog };
  }

  it("prefills the URL name from the name and says the address; the owner is resolved to a uid", async () => {
    fake.routes["POST /api/admin/orgs"] = () => json({ slug: "new-co", name: "New Co", url: "http://new-co.whygraph.localhost:8765" }, 201);
    fake.routes["/api/admin/orgs/new-co"] = () => json({ ...ORG_DETAIL, slug: "new-co", name: "New Co" });
    const { u, dialog } = await open();
    await u.type(within(dialog).getByLabelText("Organization name"), "New Co");
    expect(within(dialog).getByLabelText("URL name")).toHaveValue("new-co");
    expect(dialog).toHaveTextContent("Its address will be new-co.whygraph.localhost:8765");
    expect(dialog).toHaveTextContent("The owner must have signed in to WhyGraph with GitHub once.");
    await u.type(within(dialog).getByLabelText("Owner's GitHub username"), "@BEN");
    await u.click(within(dialog).getByRole("button", { name: "Create organization" }));
    await waitFor(() =>
      expect(fake.calls.find((c) => c.method === "POST" && c.path === "/api/admin/orgs")?.body).toEqual({
        slug: "new-co",
        name: "New Co",
        owner_uid: "u2",
      }),
    );
    expect(await screen.findByRole("heading", { level: 1, name: "New Co" })).toBeInTheDocument();
  });

  it("an unknown or deleted GitHub username is a field error and sends nothing", async () => {
    const { u, dialog } = await open();
    await u.type(within(dialog).getByLabelText("Organization name"), "X");
    await u.type(within(dialog).getByLabelText("Owner's GitHub username"), "gone");
    await u.click(within(dialog).getByRole("button", { name: "Create organization" }));
    expect(
      await within(dialog).findByText(
        "No WhyGraph account has that GitHub username. They must sign in with GitHub once, or an owner can invite them.",
      ),
    ).toBeInTheDocument();
    expect(fake.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("slug_taken and bad_slug come back from the submit as errors on URL name", async () => {
    let code = "slug_taken";
    fake.routes["POST /api/admin/orgs"] = () => json({ error: "x", code }, code === "slug_taken" ? 409 : 422);
    const { u, dialog } = await open();
    await u.type(within(dialog).getByLabelText("Organization name"), "Acme");
    await u.type(within(dialog).getByLabelText("Owner's GitHub username"), "ben");
    await u.click(within(dialog).getByRole("button", { name: "Create organization" }));
    expect(await within(dialog).findByText("That URL name is already taken.")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("URL name")).toHaveAttribute("aria-invalid", "true");
    code = "bad_slug";
    await u.click(within(dialog).getByRole("button", { name: "Create organization" }));
    await waitFor(() => expect(within(dialog).queryByText("That URL name is already taken.")).toBeNull());
  });
});

describe("Organization detail", () => {
  it("has its tabs in the URL, the Overview tiles and the stored rows", async () => {
    const router = mountAdmin("/admin/orgs/acme");
    expect(await screen.findByRole("heading", { level: 1, name: "Acme" })).toBeInTheDocument();
    expect(screen.getByTestId("tile-members")).toHaveTextContent("3");
    expect(screen.getByTestId("tile-projects")).toHaveTextContent("1");
    expect(screen.getByTestId("tile-invitations")).toHaveTextContent("1");
    expect(screen.getByTestId("tile-spend")).toHaveTextContent("$1.50");
    expect(screen.getByTestId("stored-rows")).toHaveTextContent("Usage events12");
    await userEvent.setup().click(screen.getByRole("tab", { name: "Usage" }));
    await waitFor(() => expect((router.state.location.search as { tab?: string }).tab).toBe("usage"));
    const usage = await screen.findByTestId("org-usage");
    expect(usage).toHaveTextContent("Monthly limit");
    expect(usage).toHaveTextContent("$50");
  });

  it("opens on ?tab=members, and an invalid tab falls back to Overview", async () => {
    mountAdmin("/admin/orgs/acme?tab=members");
    expect(await screen.findByTestId("org-members")).toBeInTheDocument();
  });

  it("an unknown tab shows the Overview", async () => {
    mountAdmin("/admin/orgs/acme?tab=nope");
    expect(await screen.findByTestId("org-overview")).toBeInTheDocument();
  });

  it("a missing org is a not-found that links back to the organizations", async () => {
    fake.routes["/api/admin/orgs/nope"] = () => json({ error: "no such organization" }, 404);
    mountAdmin("/admin/orgs/nope");
    expect(await screen.findByText("Not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to organizations" })).toHaveAttribute("href", "/admin/orgs");
  });

  it("the organization's address is plain text, not a link", async () => {
    mountAdmin("/admin/orgs/acme");
    expect(await screen.findByText("http://acme.whygraph.localhost:8765")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /whygraph\.localhost/ })).toBeNull();
  });
});

describe("Organization actions", () => {
  it("Rename posts the new name and shows bad_name on the field", async () => {
    let answer: Response = json({ error: "x", code: "bad_name" }, 422);
    fake.routes["PATCH /api/admin/orgs/acme"] = () => answer;
    mountAdmin("/admin/orgs/acme");
    const u = userEvent.setup();
    await u.click(await screen.findByRole("button", { name: "Rename" }));
    const dialog = await screen.findByTestId("rename-org-dialog");
    const field = within(dialog).getByLabelText("Organization name");
    await u.clear(field);
    await u.type(field, "Acme Two");
    await u.click(within(dialog).getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(fake.calls.find((c) => c.method === "PATCH")?.body).toEqual({ name: "Acme Two" }));
    expect(await within(dialog).findByRole("alert")).toBeInTheDocument();
    answer = json({ slug: "acme", name: "Acme Two" });
    await u.click(within(dialog).getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(screen.queryByTestId("rename-org-dialog")).toBeNull());
  });

  it("Open as reader shows a link after the grant, with the end time, and never opens a window", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    fake.routes["POST /api/admin/orgs/acme/reader-grant"] = () =>
      json({ id: 4, org_url: "http://acme.whygraph.localhost:8765", expires_at: "2026-11-18T09:00:00Z" });
    mountAdmin("/admin/orgs/acme");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Open as reader" }));
    const link = await screen.findByTestId("reader-link");
    expect(link).toHaveTextContent(/^Open Acme \(read-only until /);
    expect(link).toHaveAttribute("href", "http://acme.whygraph.localhost:8765");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.queryByRole("button", { name: "Open as reader" })).toBeNull();
    expect(open).not.toHaveBeenCalled();
  });

  it("Open as reader is hidden for a password account", async () => {
    fake.routes["/api/admin/me"] = () => json({ ...ME, has_password: true });
    mountAdmin("/admin/orgs/acme");
    await screen.findByRole("button", { name: "Rename" });
    expect(screen.queryByRole("button", { name: "Open as reader" })).toBeNull();
  });

  it("Suspend needs a reason (up to 500 characters) and sends it; Unsuspend asks nothing", async () => {
    fake.routes["POST /api/admin/orgs/acme/suspend"] = (_m, body) => {
      detail = { ...detail, suspended: { at: "2026-11-18T08:00:00Z", reason: (body as { reason: string }).reason } };
      return json({ slug: "acme", suspended: detail.suspended });
    };
    fake.routes["POST /api/admin/orgs/acme/unsuspend"] = () => {
      detail = { ...detail, suspended: null };
      return json({ slug: "acme", suspended: null });
    };
    mountAdmin("/admin/orgs/acme");
    const u = userEvent.setup();
    await u.click(await screen.findByRole("button", { name: "Suspend" }));
    const dialog = await screen.findByTestId("suspend-org-dialog");
    expect(dialog).toHaveTextContent("Suspend Acme?");
    expect(dialog).toHaveTextContent("Members keep read access.");
    const button = within(dialog).getByRole("button", { name: "Suspend" });
    expect(button).toBeDisabled();
    const reason = within(dialog).getByLabelText("Reason (members see it)");
    expect(reason).toHaveAttribute("maxlength", "500");
    await u.type(reason, "   ");
    expect(button).toBeDisabled();
    await u.type(reason, "Unpaid invoice");
    await u.click(button);
    await waitFor(() => expect(fake.calls.find((c) => c.path.endsWith("/suspend"))?.body).toEqual({ reason: "Unpaid invoice" }));
    const alert = await screen.findByTestId("suspended-alert");
    expect(alert).toHaveTextContent("Unpaid invoice");
    // Management stays on a suspended organization (D33).
    expect(screen.getByRole("button", { name: "Rename" })).toBeEnabled();
    await u.click(screen.getByRole("button", { name: "Unsuspend" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(screen.queryByTestId("suspended-alert")).toBeNull());
  });

  it("Delete types the slug, says what happens and nothing is erased", async () => {
    fake.routes["DELETE /api/admin/orgs/acme"] = () => json({ deleted: "acme", projects: 2 });
    mountAdmin("/admin/orgs/acme");
    const u = userEvent.setup();
    await u.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = await screen.findByTestId("delete-org-dialog");
    expect(dialog).toHaveTextContent("Delete Acme?");
    expect(dialog).toHaveTextContent("Acme disappears for its 3 members at once.");
    expect(dialog).toHaveTextContent("Nothing is erased: you can restore it, or delete it permanently later.");
    const confirm = within(dialog).getByRole("button", { name: "Delete organization" });
    expect(confirm).toBeDisabled();
    await u.type(within(dialog).getByLabelText(/to confirm/), "acme");
    await u.click(confirm);
    await waitFor(() => expect(fake.calls.find((c) => c.method === "DELETE")?.body).toEqual({ confirm_slug: "acme" }));
  });

  it("a deleted org shows the Deleted alert and only Restore and Delete permanently", async () => {
    detail = { ...detail, deleted_at: GONE, deleted_by: "u1" };
    fake.routes["POST /api/admin/orgs/acme/restore"] = () => {
      detail = { ...detail, deleted_at: null, deleted_by: null };
      return json({ restored: "acme" });
    };
    mountAdmin("/admin/orgs/acme");
    const u = userEvent.setup();
    const alert = await screen.findByTestId("deleted-alert");
    await waitFor(() => expect(alert).toHaveTextContent(/Deleted .* by Ada/));
    const actions = within(screen.getByTestId("org-actions"));
    expect(actions.getAllByRole("button").map((b) => b.textContent)).toEqual(["Restore", "Delete permanently"]);
    await u.click(actions.getByRole("button", { name: "Delete permanently" }));
    const dialog = await screen.findByTestId("purge-org-dialog");
    expect(dialog).toHaveTextContent("Delete Acme permanently?");
    expect(dialog).toHaveTextContent("and the name acme can never be used again");
    await u.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await u.click(actions.getByRole("button", { name: "Restore" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(screen.queryByTestId("deleted-alert")).toBeNull());
  });
});

describe("Members tab", () => {
  it("adds by GitHub username, resolved case-insensitively to a uid", async () => {
    fake.routes["/api/admin/users"] = () => json([...USERS, { ...USERS[1], uid: "u7", display_name: "Cy", github_login: "Cy" }]);
    fake.routes["POST /api/admin/orgs/acme/members"] = () => json({ ...ORG_DETAIL.members[1], uid: "u7" }, 201);
    mountAdmin("/admin/orgs/acme?tab=members");
    const u = userEvent.setup();
    const form = within(await screen.findByTestId("admin-add-member"));
    await waitFor(() => expect(fake.calls.some((c) => c.path === "/api/admin/users")).toBe(true));
    await u.type(form.getByLabelText("GitHub username"), "@cY");
    await u.selectOptions(form.getByLabelText("Role"), "Admin");
    await u.click(form.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(fake.calls.find((c) => c.method === "POST" && c.path.endsWith("/members"))?.body).toEqual({ uid: "u7", role: "admin" }));
  });

  it("no match is a field error and posts nothing", async () => {
    mountAdmin("/admin/orgs/acme?tab=members");
    const u = userEvent.setup();
    const form = within(await screen.findByTestId("admin-add-member"));
    await u.type(form.getByLabelText("GitHub username"), "nobody");
    await u.click(form.getByRole("button", { name: "Add" }));
    expect(
      await form.findByText("No WhyGraph account has that GitHub username. They must sign in with GitHub once, or an owner can invite them."),
    ).toBeInTheDocument();
    expect(fake.calls.some((c) => c.method === "POST" && c.path.endsWith("/members"))).toBe(false);
  });

  it("changes a role on change; a failure such as last_owner is a toast; no Owner is offered to a password account", async () => {
    let answer: Response = json({ ...ORG_DETAIL.members[1], role: "admin" });
    fake.routes["PATCH /api/admin/orgs/acme/members/u2"] = () => answer;
    mountAdmin("/admin/orgs/acme?tab=members");
    const u = userEvent.setup();
    const ben = (await screen.findAllByLabelText("Role of Ben"))[0];
    await u.selectOptions(ben, "Admin");
    await waitFor(() => expect(fake.calls.find((c) => c.method === "PATCH")?.body).toEqual({ role: "admin" }));
    answer = json({ error: "x", code: "last_owner" }, 409);
    await u.selectOptions(ben, "Member");
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    const rootOptions = within((await screen.findAllByLabelText("Role of Root"))[0]).getAllByRole("option").map((o) => o.textContent);
    expect(rootOptions).toEqual(["Member", "Admin"]);
    const adaOptions = within((await screen.findAllByLabelText("Role of Ada"))[0]).getAllByRole("option").map((o) => o.textContent);
    expect(adaOptions).toContain("Owner");
  });

  it("removes a member after a confirmation that names them and the org", async () => {
    fake.routes["DELETE /api/admin/orgs/acme/members/u2"] = () => new Response(null, { status: 204 });
    mountAdmin("/admin/orgs/acme?tab=members");
    const u = userEvent.setup();
    const row = within((await screen.findAllByTestId("org-member-u2"))[0]);
    await u.click(row.getByRole("button", { name: "Remove" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Remove Ben from Acme?");
    expect(dialog).toHaveTextContent("They lose access to its projects at once.");
    await u.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(fake.calls.some((c) => c.method === "DELETE" && c.path.endsWith("/members/u2"))).toBe(true));
  });

  it("a deleted org has no member controls", async () => {
    detail = { ...detail, deleted_at: GONE, deleted_by: "u1" };
    mountAdmin("/admin/orgs/acme?tab=members");
    await screen.findByTestId("org-members");
    expect(screen.queryByTestId("admin-add-member")).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();
    expect(screen.queryByLabelText("Role of Ben")).toBeNull();
  });
});

describe("Projects tab", () => {
  it("a live project has Delete (the name typed), a deleted one Restore and Delete permanently", async () => {
    fake.routes["DELETE /api/admin/projects/acme/api"] = () => json({ removed: "api", soft: true });
    fake.routes["POST /api/admin/projects/acme/old/restore"] = () => json({ restored: "old" });
    mountAdmin("/admin/orgs/acme?tab=projects");
    const u = userEvent.setup();
    const api = within((await screen.findAllByTestId("org-project-api"))[0]);
    expect(api.queryByRole("button", { name: "Restore" })).toBeNull();
    const old = within(screen.getAllByTestId("org-project-old")[0]);
    expect(old.getByRole("button", { name: "Restore" })).toBeEnabled();
    expect(old.getByRole("button", { name: "Delete permanently" })).toBeInTheDocument();
    expect(old.queryByRole("button", { name: "Delete" })).toBeNull();
    await u.click(api.getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByTestId("delete-project-dialog");
    expect(dialog).toHaveTextContent("Delete Api?");
    expect(dialog).toHaveTextContent("Api disappears for everyone in Acme at once, and its scans stop.");
    await u.type(within(dialog).getByLabelText(/to confirm/), "Api");
    await u.click(within(dialog).getByRole("button", { name: "Delete project" }));
    await waitFor(() => expect(fake.calls.find((c) => c.method === "DELETE")?.body).toEqual({ confirm_name: "Api" }));
    await u.click(old.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(fake.calls.some((c) => c.path.endsWith("/old/restore"))).toBe(true));
  });

  it("purges a project with its name typed", async () => {
    fake.routes["POST /api/admin/projects/acme/old/purge"] = () => json({ removed: "old" });
    mountAdmin("/admin/orgs/acme?tab=projects");
    const u = userEvent.setup();
    await u.click(within((await screen.findAllByTestId("org-project-old"))[0]).getByRole("button", { name: "Delete permanently" }));
    const dialog = await screen.findByTestId("purge-project-dialog");
    expect(dialog).toHaveTextContent("Delete Old permanently?");
    await u.type(within(dialog).getByLabelText(/to confirm/), "Old");
    await u.click(within(dialog).getByRole("button", { name: "Delete permanently" }));
    await waitFor(() => expect(fake.calls.find((c) => c.path.endsWith("/old/purge"))?.body).toEqual({ confirm_name: "Old" }));
  });

  it("in a deleted org Restore is disabled with its reason and Delete is gone", async () => {
    detail = { ...detail, deleted_at: GONE, deleted_by: "u1" };
    mountAdmin("/admin/orgs/acme?tab=projects");
    const old = within((await screen.findAllByTestId("org-project-old"))[0]);
    expect(old.getByRole("button", { name: "Restore" })).toBeDisabled();
    expect(old.getByText("Restore the organization first.")).toBeInTheDocument();
    const api = within(screen.getAllByTestId("org-project-api")[0]);
    expect(api.queryByRole("button", { name: "Delete" })).toBeNull();
  });
});

describe("Cross-org audit", () => {
  const event = (id: number, org: string | null) => ({
    id,
    created_at: "2026-10-05T10:00:00Z",
    org,
    actor: null,
    event: org ? "member_added" : "github_signin",
    target: null,
    ip: null,
    fields: {},
  });
  const auditCalls = () => fake.calls.filter((c) => c.path === "/api/admin/audit");

  beforeEach(() => {
    lists([{ ...ORGS[0], slug: "dead", name: "Dead", deleted_at: GONE, deleted_by: "u1" }]);
    fake.routes["/api/admin/audit"] = () => json({ events: [event(1, "acme"), event(2, null)], next: null });
  });

  it("offers All organizations, Platform events, then every org by name with deleted ones suffixed", async () => {
    mountAdmin("/admin/audit");
    const select = await screen.findByLabelText("Organization");
    await waitFor(() => expect(within(select).getAllByRole("option").length).toBe(4));
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "All organizations",
      "Platform events (no organization)",
      "Acme",
      "Dead (deleted)",
    ]);
  });

  it("applying the filter writes it to the URL and sends scope and org; platform events send scope=orgless", async () => {
    const router = mountAdmin("/admin/audit");
    const u = userEvent.setup();
    const select = await screen.findByLabelText("Organization");
    await waitFor(() => expect(within(select).getAllByRole("option").length).toBe(4));
    await u.selectOptions(select, "Acme");
    await u.click(screen.getByRole("button", { name: "Filter" }));
    await waitFor(() => expect(router.state.location.search).toEqual({ org: "acme" }));
    await waitFor(() => expect(auditCalls().at(-1)?.search).toBe("?org=acme&scope=all"));
    await u.selectOptions(select, "Platform events (no organization)");
    await u.click(screen.getByRole("button", { name: "Filter" }));
    await waitFor(() => expect(router.state.location.search).toEqual({ scope: "orgless" }));
    await waitFor(() => expect(auditCalls().at(-1)?.search).toBe("?scope=orgless"));
  });

  it("opens with the filters of the URL, and the actor link of a user lands filled in", async () => {
    mountAdmin("/admin/audit?org=acme&actor=%40ben");
    expect(await screen.findByLabelText("Actor")).toHaveValue("@ben");
    expect(screen.getByLabelText("Organization")).toHaveValue("acme");
    await waitFor(() => expect(auditCalls().at(-1)?.search).toBe("?org=acme&actor=%40ben&scope=all"));
  });

  it("names each event's organization in a column after Event, and '-' for none", async () => {
    mountAdmin("/admin/audit");
    const table = within(await screen.findByTestId("audit-table"));
    const headers = (await table.findAllByRole("columnheader")).map((h) => h.textContent);
    expect(headers.slice(0, 3)).toEqual(["Event", "Organization", "When"]);
    expect(table.getAllByText("Acme").length).toBeGreaterThan(0);
  });

  it("downloads the admin CSV with the same filters", async () => {
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => {} }));
    fake.routes["/api/admin/audit.csv"] = () => new Response("a,b", { headers: { "content-type": "text/csv" } });
    mountAdmin("/admin/audit?org=acme");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Download CSV" }));
    await waitFor(() => expect(fake.calls.some((c) => c.path === "/api/admin/audit.csv" && c.search.includes("org=acme"))).toBe(true));
  });

  it("an org's Audit tab locks the organization: no org select, the org in every request", async () => {
    mountAdmin("/admin/orgs/acme?tab=audit");
    await screen.findByTestId("audit-table");
    expect(screen.queryByLabelText("Organization")).toBeNull();
    await waitFor(() => expect(auditCalls().at(-1)?.search).toBe("?org=acme&scope=all"));
    expect(screen.queryByRole("columnheader", { name: "Organization" })).toBeNull();
  });
});
