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

  it("lists @login or email by uid and links each row to its detail page", async () => {
    mountAdmin("/admin/users");
    const ben = within(await screen.findByTestId("admin-user-u2"));
    expect(ben.getByText("@ben")).toBeInTheDocument();
    expect(ben.getByRole("link", { name: /Ben/ })).toHaveAttribute("href", "/admin/users/u2");
    expect(within(screen.getByTestId("admin-user-u3")).getByText("root@example.com")).toBeInTheDocument();
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
  it("lists name, slug, members, projects, owners and spend, the address as no link", async () => {
    mountAdmin("/admin/orgs");
    const row = within(await screen.findByTestId("admin-org-acme"));
    expect(row.getByText("Acme")).toBeInTheDocument();
    expect(row.getByText("acme")).toBeInTheDocument();
    expect(row.getByText("3 members")).toBeInTheDocument();
    expect(row.getByText("2 projects")).toBeInTheDocument();
    expect(row.getByText("@ada")).toBeInTheDocument();
    expect(row.getByText("$1.50")).toBeInTheDocument();
    expect(row.queryByRole("link", { name: /whygraph\.localhost/ })).toBeNull();
  });

  it("says what a search found nothing for", async () => {
    mountAdmin("/admin/orgs?q=zzz");
    expect(await screen.findByText("No organization matches.")).toBeInTheDocument();
  });

  it("says what an empty platform needs", async () => {
    fake.routes["/api/admin/orgs"] = () => json([]);
    mountAdmin("/admin/orgs");
    expect(await screen.findByText("No organizations yet")).toBeInTheDocument();
    expect(screen.getByText(/or use Create organization/)).toBeInTheDocument();
  });
});

describe("Audit", () => {
  it("lists the cross-org events with an Organization column, and offers the CSV", async () => {
    fake.routes["/api/admin/audit"] = () =>
      json({
        events: [
          { id: 5, created_at: "2026-10-05T10:00:00Z", org: null, actor: null, event: "github_signin", target: null, ip: null, fields: {} },
          { id: 6, created_at: "2026-10-05T11:00:00Z", org: "acme", actor: null, event: "member_added", target: null, ip: null, fields: {} },
        ],
        next: null,
      });
    mountAdmin("/admin/audit");
    const box = within(await screen.findByTestId("admin-audit"));
    expect(await box.findByText("Signed in with GitHub")).toBeInTheDocument();
    expect(box.getAllByText("System").length).toBeGreaterThan(0);
    expect(box.getByRole("columnheader", { name: "Organization" })).toBeInTheDocument();
    expect(box.getAllByText("Acme").length).toBeGreaterThan(0);
    expect(box.getByRole("button", { name: "Download CSV" })).toBeInTheDocument();
    expect(fake.calls.find((c) => c.path === "/api/admin/audit")?.search).toBe("?scope=all");
  });
});

it("keeps the shared fixtures in step with /api/admin/me", () => {
  expect(USERS.find((u) => u.uid === ME.uid)?.github_login).toBe(ME.github_login);
});
