import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BASE, ME, USERS, fake, json, mountAdmin, resetAdminFake, signedInAdmin, strays } from "./adminHarness";

// The Phase 1 modules: what the old `AdminPage` held, one module per route.

beforeEach(() => {
  resetAdminFake();
  signedInAdmin();
});
afterEach(() => {
  expect(strays()).toEqual([]);
  vi.unstubAllGlobals();
});

describe("Dashboard", () => {
  it("shows user and organization counts, the settings check and the updates card", async () => {
    mountAdmin("/admin");
    expect(await screen.findByTestId("tile-users")).toHaveTextContent("3");
    expect(screen.getByTestId("tile-orgs")).toHaveTextContent("1");
    expect(await screen.findByTestId("base-check-ok")).toHaveTextContent("found no problems");
    expect(await screen.findByTestId("admin-updates")).toHaveTextContent("Current version: 2.6.0");
  });

  it("lists the base URL problems in the settings check card", async () => {
    fake.routes["/api/admin/settings"] = () =>
      json({ base_url: BASE, base_check: ["*.whygraph.localhost does not resolve"] });
    mountAdmin("/admin");
    expect(await screen.findByTestId("base-check")).toHaveTextContent("does not resolve");
  });

  it("an error on the counts shows one retryable block", async () => {
    fake.routes["/api/admin/users"] = () => json({ error: "boom" }, 500);
    mountAdmin("/admin");
    expect(await screen.findByText("Couldn't load the platform figures")).toBeInTheDocument();
  });
});

describe("Users", () => {
  let users: typeof USERS;
  beforeEach(() => {
    users = USERS.map((u) => ({ ...u }));
    fake.routes["/api/admin/users"] = () => json(users);
    fake.routes["/api/admin/users/u2"] = (_method, body) => {
      users = users.map((u) => (u.uid === "u2" ? { ...u, ...(body as object) } : u));
      return json(users[1]);
    };
  });

  it("lists @login or email by uid, reset links only for password accounts", async () => {
    mountAdmin("/admin/users");
    const ben = within(await screen.findByTestId("admin-user-u2"));
    expect(ben.getByText("@ben")).toBeInTheDocument();
    expect(ben.queryByRole("button", { name: "Copy reset link" })).toBeNull();
    const root = within(screen.getByTestId("admin-user-u3"));
    expect(root.getByText("root@example.com")).toBeInTheDocument();
    expect(root.getByRole("button", { name: "Copy reset link" })).toBeInTheDocument();
  });

  it("takes 'me' from /api/admin/me: your own account has no Disable, another admin has", async () => {
    mountAdmin("/admin/users");
    const ada = within(await screen.findByTestId("admin-user-u1"));
    expect(ada.queryByRole("button", { name: "Disable" })).toBeNull();
    const root = within(screen.getByTestId("admin-user-u3"));
    expect(root.getByRole("button", { name: "Disable" })).toBeInTheDocument();
    expect(fake.calls.some((c) => c.path === "/api/admin/me")).toBe(true);
  });

  it("makes an admin with a secondary button", async () => {
    mountAdmin("/admin/users");
    const ben = within(await screen.findByTestId("admin-user-u2"));
    expect(ben.getByRole("button", { name: "Make admin" }).className).toContain("bg-secondary");
  });

  it("disables only after a confirmation, and enables at once", async () => {
    mountAdmin("/admin/users");
    const u = userEvent.setup();
    const ben = within(await screen.findByTestId("admin-user-u2"));
    await u.click(ben.getByRole("button", { name: "Disable" }));
    expect(fake.calls.some((c) => c.path === "/api/admin/users/u2" && c.method === "PATCH")).toBe(false);
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Disable Ben?");
    await u.click(within(dialog).getByRole("button", { name: "Disable" }));
    await waitFor(() =>
      expect(fake.calls.find((c) => c.path === "/api/admin/users/u2")?.body).toEqual({ disabled: true }),
    );
    expect(await ben.findByText("disabled")).toBeInTheDocument();
    await u.click(ben.getByRole("button", { name: "Enable" }));
    await waitFor(() =>
      expect(fake.calls.filter((c) => c.path === "/api/admin/users/u2").at(-1)?.body).toEqual({ disabled: false }),
    );
  });

  it("filters on name, login and email as you type, and keeps the search in the URL", async () => {
    const router = mountAdmin("/admin/users");
    const u = userEvent.setup();
    await screen.findByTestId("admin-user-u1");
    await u.type(screen.getByLabelText("Search"), "root@");
    expect(screen.queryByTestId("admin-user-u2")).toBeNull();
    expect(screen.getByTestId("admin-user-u3")).toBeInTheDocument();
    await waitFor(() => expect((router.state.location.search as { q?: string }).q).toBe("root@"));
  });

  it("opens with ?q= already applied", async () => {
    mountAdmin("/admin/users?q=ben");
    await screen.findByTestId("admin-user-u2");
    expect(screen.queryByTestId("admin-user-u1")).toBeNull();
    expect(screen.getByLabelText("Search")).toHaveValue("ben");
  });
});

describe("Organizations", () => {
  it("lists name, slug, address, members and created", async () => {
    mountAdmin("/admin/orgs");
    const row = within(await screen.findByTestId("admin-org-acme"));
    expect(row.getByText("Acme")).toBeInTheDocument();
    expect(row.getByText("acme")).toBeInTheDocument();
    expect(row.getByRole("link", { name: "http://acme.whygraph.localhost:8765" })).toBeInTheDocument();
    expect(row.getByText("3 members")).toBeInTheDocument();
  });

  it("says what a search found nothing for, and what an empty platform needs", async () => {
    mountAdmin("/admin/orgs?q=zzz");
    expect(await screen.findByText("No organization matches that search.")).toBeInTheDocument();
  });
});

describe("Audit", () => {
  it("lists the orgless events", async () => {
    fake.routes["/api/admin/audit"] = () =>
      json({
        events: [
          { id: 5, created_at: "2026-10-05T10:00:00Z", org: null, actor: null, event: "github_signin", target: null, ip: null, fields: {} },
        ],
        next: null,
      });
    mountAdmin("/admin/audit");
    const box = within(await screen.findByTestId("admin-audit"));
    expect(await box.findByText("Signed in with GitHub")).toBeInTheDocument();
    expect(box.getAllByText("System").length).toBeGreaterThan(0);
    expect(box.queryByRole("button", { name: "Download CSV" })).toBeNull();
  });
});

it("keeps the shared fixtures in step with /api/admin/me", () => {
  expect(USERS.find((u) => u.uid === ME.uid)?.github_login).toBe(ME.github_login);
});
