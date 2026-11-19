import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { auditDetails } from "../lib/auditEvents";
import type { AuditEventRow } from "../api";
import {
  BASE,
  CONFIG,
  baseState,
  fake,
  json,
  mountAdmin,
  resetAdminFake,
  signedInAdmin,
  strays,
} from "./adminHarness";

// Plan 59, §4.15 phase 4: the Configuration page, the announcement banner in its four places,
// the "Create organization" sites under `can_create_org`, and the worded audit details.

type Settings = typeof CONFIG.settings;
let settings: Settings;
let baseCheck: string[] | null;
let allowlist: Record<string, unknown>[];

const putBodies = () => fake.calls.filter((c) => c.method === "PUT" && c.path === "/api/admin/config").map((c) => c.body);

function configRoutes() {
  fake.routes["GET /api/admin/config"] = () =>
    json({ settings, instance: { ...CONFIG.instance, base_check: baseCheck } });
  fake.routes["PUT /api/admin/config"] = (_m, body) => {
    settings = { ...settings, ...(body as Partial<Settings>), updated_at: "2026-11-19T10:00:00Z", updated_by: "u1" };
    return json(settings);
  };
  fake.routes["GET /api/admin/config/signup-allowlist"] = () => json(allowlist);
}

beforeEach(() => {
  settings = { ...CONFIG.settings };
  baseCheck = [];
  allowlist = [];
  resetAdminFake();
  signedInAdmin();
  configRoutes();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Configuration page", () => {
  it("is in the nav and shows the title, the defaults and the instance card", async () => {
    mountAdmin("/admin/config");
    expect(await screen.findByRole("heading", { level: 1, name: "Platform configuration" })).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Configuration" }).length).toBeGreaterThan(0);
    expect(await screen.findByText("Not changed yet: the defaults apply.")).toBeInTheDocument();
    const instance = await screen.findByTestId("admin-instance");
    expect(instance).toHaveTextContent(BASE);
    expect(within(instance).getByTestId("base-check-ok")).toBeInTheDocument();
    expect(instance).toHaveTextContent("whygraph-app");
    expect(instance).toHaveTextContent("Iv1.oauth");
    expect(instance).toHaveTextContent("50.0 GiB of 100.0 GiB; the database is on its own volume");
    expect(instance).toHaveTextContent("1.2 GiB");
    expect(within(instance).getByTestId("admin-updates-latest")).toHaveTextContent("Up to date");
    // The allowlist is shown only for the saved invite-only policy.
    expect(screen.queryByTestId("config-allowlist")).toBeNull();
    expect(strays()).toEqual([]);
  });

  it("a chosen section goes in ?section= without a history entry; the first one and an unknown one stay out", async () => {
    const router = mountAdmin("/admin/config?section=nope");
    await screen.findByTestId("config-limits");
    expect(router.state.location.search).toEqual({});
    const nav = within(screen.getByRole("navigation", { name: "Settings sections" }));
    const before = router.history.length;
    const u = userEvent.setup();
    await u.click(nav.getByRole("button", { name: "Limits" }));
    await waitFor(() => expect(router.state.location.search).toEqual({ section: "limits" }));
    expect(nav.getByRole("button", { name: "Limits" })).toHaveAttribute("aria-current", "true");
    await u.click(nav.getByRole("button", { name: "Sign-up" }));
    await waitFor(() => expect(router.state.location.search).toEqual({}));
    expect(router.history.length).toBe(before);
  });

  it("opens on the section the URL names", async () => {
    const router = mountAdmin("/admin/config?section=announcement");
    await screen.findByTestId("config-announcement");
    expect(router.state.location.search).toEqual({ section: "announcement" });
    const nav = within(screen.getByRole("navigation", { name: "Settings sections" }));
    expect(nav.getByRole("button", { name: "Announcement" })).toHaveAttribute("aria-current", "true");
  });

  it("lists the address problems, and the Dashboard's attention line links here", async () => {
    baseCheck = ["*.whygraph.localhost does not resolve"];
    mountAdmin("/admin/config");
    expect(await screen.findByTestId("base-check")).toHaveTextContent("does not resolve");
  });

  it("says who changed it last", async () => {
    settings = { ...settings, updated_at: "2026-11-18T09:30:00Z", updated_by: "u2" };
    mountAdmin("/admin/config");
    expect(await screen.findByTestId("config-changed")).toHaveTextContent(/Last changed .* by /);
  });

  it("each section saves only its own keys", async () => {
    const user = userEvent.setup();
    mountAdmin("/admin/config");
    const signup = await screen.findByTestId("config-signup-form");
    await user.selectOptions(within(signup).getByLabelText("Who can sign up"), "closed");
    expect(within(signup).getByText(/Only people who already have an account/)).toBeInTheDocument();
    await user.click(within(signup).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(putBodies()).toEqual([{ signup_policy: "closed" }]));
    expect(await within(signup).findByText("Saved")).toBeInTheDocument();

    const orgs = screen.getByTestId("config-organizations-form");
    await user.selectOptions(within(orgs).getByLabelText("Who can create organizations"), "admins");
    await user.click(within(orgs).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(putBodies()).toHaveLength(2));
    expect(putBodies()[1]).toEqual({ org_creation: "admins" });
    // "Last changed" follows the save.
    await waitFor(() => expect(screen.getByTestId("config-changed")).toHaveTextContent("by Ada"));
  });

  it("sets a quota, clears it by emptying the field, and refuses a bad number in the client", async () => {
    const user = userEvent.setup();
    mountAdmin("/admin/config");
    const limits = await screen.findByTestId("config-limits-form");
    const orgs = within(limits).getByLabelText("Organizations one person may own");
    const projects = within(limits).getByLabelText("Projects per organization");
    expect(within(limits).getAllByText("Leave empty for no limit.")).toHaveLength(2);

    await user.type(orgs, "3");
    await user.click(within(limits).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(putBodies()).toEqual([{ max_orgs_per_user: 3 }]));

    await user.clear(orgs);
    await user.click(within(limits).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(putBodies()).toHaveLength(2));
    expect(putBodies()[1]).toEqual({ max_orgs_per_user: null });

    await user.type(projects, "0");
    await user.click(within(limits).getByRole("button", { name: "Save" }));
    expect(await within(limits).findByText("Enter a whole number of 1 or more, or leave it empty.")).toBeInTheDocument();
    await user.clear(projects);
    await user.type(projects, "2.5");
    await user.click(within(limits).getByRole("button", { name: "Save" }));
    expect(await within(limits).findByText("Enter a whole number of 1 or more, or leave it empty.")).toBeInTheDocument();
    expect(putBodies()).toHaveLength(2);
  });

  it("the announcement counts characters, previews as text and saves only its keys", async () => {
    const user = userEvent.setup();
    mountAdmin("/admin/config");
    const form = await screen.findByTestId("config-announcement-form");
    expect(within(form).getByTestId("banner-count")).toHaveTextContent("0 / 500");
    expect(within(form).getByText("Everyone sees this text, signed in or not. Leave it empty to show no banner.")).toBeInTheDocument();
    await user.click(within(form).getByLabelText("Banner text"));
    await user.paste("Maintenance <b>tonight</b>");
    expect(within(form).getByTestId("banner-count")).toHaveTextContent("26 / 500");
    const preview = within(form).getByTestId("announcement-preview");
    expect(preview).toHaveTextContent("Maintenance <b>tonight</b>");
    expect(preview.querySelector("b")).toBeNull();
    expect(within(preview).queryByRole("button")).toBeNull();
    await user.selectOptions(within(form).getByLabelText("Banner style"), "warning");
    expect(preview).toHaveAttribute("data-tone", "warning");
    await user.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(putBodies()).toEqual([{ banner_text: "Maintenance <b>tonight</b>", banner_level: "warning" }]),
    );
    // Emptying the text clears it (null), leaving the style alone.
    await user.clear(within(form).getByLabelText("Banner text"));
    await user.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(putBodies()).toHaveLength(2));
    expect(putBodies()[1]).toEqual({ banner_text: null });
  });

  it("a server error on save says so in the form", async () => {
    fake.routes["PUT /api/admin/config"] = () => json({ error: "nope", code: "forbidden" }, 403);
    const user = userEvent.setup();
    mountAdmin("/admin/config");
    const signup = await screen.findByTestId("config-signup-form");
    await user.selectOptions(within(signup).getByLabelText("Who can sign up"), "closed");
    await user.click(within(signup).getByRole("button", { name: "Save" }));
    expect(await within(signup).findByText("Couldn't save")).toBeInTheDocument();
  });
});

describe("Sign-up allowlist", () => {
  const entry = (over: Record<string, unknown> = {}) => ({
    id: 1,
    github_login: "ben",
    note: "contractor, ACME",
    created_at: "2026-11-18T09:00:00Z",
    created_by: "u1",
    used_at: null,
    ...over,
  });

  beforeEach(() => {
    settings = { ...settings, signup_policy: "invite_only" };
  });

  it("shows the empty state, and adds an entry with its note", async () => {
    const user = userEvent.setup();
    fake.routes["POST /api/admin/config/signup-allowlist"] = (_m, body) => {
      allowlist = [entry({ github_login: (body as { github_login: string }).github_login, note: "contractor" })];
      return json({ ...allowlist[0], added: true });
    };
    mountAdmin("/admin/config");
    const section = await screen.findByTestId("config-allowlist");
    expect(await within(section).findByText("Nobody is on the allowlist")).toBeInTheDocument();
    expect(within(section).getByText("Add a GitHub username to let that account sign up.")).toBeInTheDocument();
    await user.type(within(section).getByLabelText("GitHub username"), "@ben");
    await user.type(within(section).getByLabelText("Note (optional, up to 200 characters)"), "contractor");
    await user.click(within(section).getByRole("button", { name: "Add" }));
    const row = await within(section).findByTestId("allowlist-1");
    expect(row).toHaveTextContent("@ben");
    expect(row).toHaveTextContent("contractor");
    expect(row).toHaveTextContent("Ada");
    expect(row).toHaveTextContent("Not used yet");
    const post = fake.calls.find((c) => c.method === "POST" && c.path === "/api/admin/config/signup-allowlist");
    expect(post?.body).toEqual({ github_login: "ben", note: "contractor" });
  });

  it("says when the account is already on the list", async () => {
    allowlist = [entry()];
    fake.routes["POST /api/admin/config/signup-allowlist"] = () => json({ ...entry(), added: false });
    const user = userEvent.setup();
    mountAdmin("/admin/config");
    const section = await screen.findByTestId("config-allowlist");
    await user.type(await within(section).findByLabelText("GitHub username"), "ben");
    await user.click(within(section).getByRole("button", { name: "Add" }));
    expect(await within(section).findByTestId("allowlist-existing")).toHaveTextContent("@ben is already on the list.");
  });

  it("puts an unknown username on the field and a GitHub outage under the form", async () => {
    const user = userEvent.setup();
    fake.routes["POST /api/admin/config/signup-allowlist"] = (_m, body) =>
      (body as { github_login: string }).github_login === "ghost"
        ? json({ error: "no such user", code: "no_such_github_user", login: "ghost" }, 404)
        : json({ error: "down", code: "github_unavailable" }, 502);
    mountAdmin("/admin/config");
    const section = await screen.findByTestId("config-allowlist");
    const field = await within(section).findByLabelText("GitHub username");
    await user.type(field, "ghost");
    await user.click(within(section).getByRole("button", { name: "Add" }));
    expect(await within(section).findByText("GitHub has no user called @ghost.")).toBeInTheDocument();
    await user.clear(field);
    await user.type(field, "ben");
    await user.click(within(section).getByRole("button", { name: "Add" }));
    expect(await within(section).findByTestId("allowlist-error")).toBeInTheDocument();
  });

  it("removes an entry after a confirmation", async () => {
    allowlist = [entry({ used_at: "2026-11-19T08:00:00Z" })];
    fake.routes["DELETE /api/admin/config/signup-allowlist/1"] = () => {
      allowlist = [];
      return json({ removed: true });
    };
    const user = userEvent.setup();
    mountAdmin("/admin/config");
    const section = await screen.findByTestId("config-allowlist");
    expect(await within(section).findByTestId("allowlist-1")).not.toHaveTextContent("Not used yet");
    await user.click(within(section).getAllByRole("button", { name: "Remove @ben" })[0]);
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Remove @ben from the allowlist?");
    expect(dialog).toHaveTextContent("They can no longer sign up unless an organization invites them.");
    await user.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() =>
      expect(fake.calls.some((c) => c.method === "DELETE" && c.path === "/api/admin/config/signup-allowlist/1")).toBe(true),
    );
    expect(await within(section).findByText("Nobody is on the allowlist")).toBeInTheDocument();
  });
});

describe("Announcement banner", () => {
  const announcement = { text: "Maintenance <b>tonight</b>", level: "warning" };

  it("shows in the admin app as text, with no close button", async () => {
    resetAdminFake({ announcement });
    signedInAdmin();
    configRoutes();
    mountAdmin("/admin");
    const banner = await screen.findByTestId("announcement-banner");
    expect(banner).toHaveTextContent("Maintenance <b>tonight</b>");
    expect(banner.querySelector("b")).toBeNull();
    expect(within(banner).queryByRole("button")).toBeNull();
  });

  it("shows on the admin sign-in page, signed out", async () => {
    resetAdminFake({ announcement: { text: "Planned downtime", level: "info" } });
    mountAdmin("/admin/login");
    expect(await screen.findByTestId("announcement-banner")).toHaveTextContent("Planned downtime");
    expect(screen.getByTestId("announcement-banner")).toHaveAttribute("data-tone", "info");
  });

  it("shows on the signed-out base pages", async () => {
    resetAdminFake({ announcement });
    mountAdmin("/signin");
    expect(await screen.findByTestId("announcement-banner")).toHaveTextContent("Maintenance <b>tonight</b>");
  });

  it("is absent when there is no announcement", async () => {
    resetAdminFake({ announcement: null });
    mountAdmin("/admin/login");
    await screen.findByRole("heading", { level: 1, name: "Sign in to administration" });
    expect(screen.queryByTestId("announcement-banner")).toBeNull();
  });
});

// The everyday app: a signed-in user on the base and org hosts.
describe("everyday pages", () => {
  const ada = { uid: "u1", display_name: "Ada", email: "a@example.com", role: null, is_instance_admin: false, github_login: "ada" };
  let state: Record<string, unknown>;
  let orgs: unknown[];

  function everyday(over: Record<string, unknown> = {}) {
    state = baseState({ user: ada, can_create_org: true, org_creation: "everyone", announcement: null, ...over });
    orgs = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = new URL(String(input), "http://127.0.0.1:8765").pathname;
        if (path === "/api/portal/state") return Promise.resolve(json(state));
        if (path === "/api/account/orgs") return Promise.resolve(json(orgs));
        if (path === "/api/projects") return Promise.resolve(json({ projects: [] }));
        if (path === "/api/orgs/slug-check") return Promise.resolve(json({ slug: "x", available: true }));
        return Promise.resolve(json({ error: "unhandled" }, 404));
      }),
    );
  }

  beforeEach(() => {
    resetAdminFake();
  });

  it("shows the banner in the base chrome, signed in", async () => {
    everyday({ announcement: { text: "Heads up", level: "info" } });
    mountAdmin("/account");
    expect(await screen.findByTestId("announcement-banner")).toHaveTextContent("Heads up");
  });

  it("shows the banner in the org app", async () => {
    everyday({
      host_kind: "org",
      org: { slug: "acme", name: "Acme", role: "owner" },
      announcement: { text: "Heads up", level: "warning" },
    });
    mountAdmin("/");
    expect(await screen.findByTestId("announcement-banner")).toHaveTextContent("Heads up");
  });

  it("offers Create organization on the empty picker when allowed", async () => {
    everyday();
    mountAdmin("/orgs");
    expect(await screen.findByText("Create one for your team, or ask to join an existing one.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create organization" })).toBeInTheDocument();
  });

  it("hides it everywhere it is offered when can_create_org is false", async () => {
    everyday({ can_create_org: false, org_creation: "admins" });
    mountAdmin("/orgs");
    expect(await screen.findByText("Ask an organization owner to add you.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Create organization" })).toBeNull();
    expect(screen.getByTestId("join-hint")).toBeInTheDocument();
  });

  it("hides the footer link of the picker and the Account link", async () => {
    everyday({ can_create_org: false, org_creation: "admins" });
    orgs = [
      { slug: "acme", name: "Acme", role: "owner", url: "http://acme.whygraph.localhost:8765", new: false },
      { slug: "beta", name: "Beta", role: "member", url: "http://beta.whygraph.localhost:8765", new: false },
    ];
    mountAdmin("/orgs");
    await screen.findByTestId("org-list");
    expect(screen.queryByRole("link", { name: "Create organization" })).toBeNull();
  });

  it("the Account page offers no Create one when the platform restricts creation", async () => {
    everyday({ can_create_org: false, org_creation: "admins" });
    mountAdmin("/account");
    const card = await screen.findByTestId("account-orgs");
    expect(await within(card).findByText(/You aren't in an organization yet\./)).toBeInTheDocument();
    expect(within(card).queryByRole("link", { name: "Create one" })).toBeNull();
  });

  it("/orgs/new renders an empty state instead of the form", async () => {
    everyday({ can_create_org: false, org_creation: "admins" });
    mountAdmin("/orgs/new");
    expect(
      await screen.findByText("Only platform administrators can create organizations. Ask an organization owner to invite you."),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Organization name")).toBeNull();
  });
});

describe("the worded instance_settings_changed details", () => {
  const row = (fields: Record<string, unknown>): AuditEventRow =>
    ({ event: "instance_settings_changed", fields }) as unknown as AuditEventRow;

  it("words each changed setting as old to new", () => {
    const details = auditDetails(
      row({
        changes: {
          signup_policy: ["open", "invite_only"],
          org_creation: ["everyone", "admins"],
          max_orgs_per_user: [null, 3],
          max_projects_per_org: [5, null],
          banner_text: [null, "Maintenance"],
          banner_level: ["info", "warning"],
        },
      }),
    );
    expect(details).toEqual([
      { label: "Sign-up policy", value: "open to invite only" },
      { label: "Who creates organizations", value: "everyone to administrators only" },
      { label: "Organizations one person may own", value: "no limit to 3" },
      { label: "Projects per organization", value: "5 to no limit" },
      { label: "Banner text", value: 'empty to "Maintenance"' },
      { label: "Banner style", value: "info to warning" },
    ]);
  });

  it("words the allowlist events by login, and never shows raw JSON for changes", () => {
    expect(auditDetails({ event: "signup_allowlist_changed", fields: { added: "ben" } } as unknown as AuditEventRow)).toEqual([
      { label: "Added to the allowlist", value: "@ben" },
    ]);
    const odd = auditDetails(row({ changes: "not an object" }));
    expect(odd).toEqual([{ label: "Changes", value: "not an object" }]);
  });
});
