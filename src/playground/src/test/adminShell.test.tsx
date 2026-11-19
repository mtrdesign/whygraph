import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { adminCrumbs } from "../components/admin/adminNav";
import { BASE, ME, ORGS, USERS, fake, hard, json, mountAdmin, resetAdminFake, signedInAdmin, strays } from "./adminHarness";

beforeEach(() => resetAdminFake());
afterEach(() => {
  // The admin app calls only /api/admin/*, the admin sign-in routes and /api/portal/state.
  expect(strays()).toEqual([]);
  vi.unstubAllGlobals();
});

const nav = () => screen.getByRole("navigation", { name: "Main" });

describe("admin shell: session", () => {
  it("a first visit's 401 goes to /admin/login with next and WITHOUT reauth", async () => {
    fake.routes["/api/admin/me"] = () => json({ error: "x", code: "admin_login_required" }, 401);
    mountAdmin("/admin");
    await waitFor(() => expect(hard).toHaveBeenCalledTimes(1));
    const target = String(hard.mock.calls[0][0]);
    expect(target.startsWith(`${BASE}/admin/login?next=`)).toBe(true);
    expect(target).not.toContain("reauth");
    expect(new URL(target).searchParams.get("next")).toBe(window.location.href);
  });

  it("a 401 after the session was seen adds reauth=1", async () => {
    signedInAdmin();
    mountAdmin("/admin");
    await screen.findByRole("heading", { name: "Dashboard" });
    fake.routes["/api/admin/audit"] = () => json({ error: "x", code: "admin_login_required" }, 401);
    await userEvent.setup().click(within(nav()).getByRole("link", { name: "Audit" }));
    await waitFor(() => expect(hard).toHaveBeenCalled());
    expect(String(hard.mock.calls.at(-1)?.[0])).toMatch(/\/admin\/login\?next=.*&reauth=1$/);
  });

  it("signs out through the admin route and follows its redirect", async () => {
    signedInAdmin();
    fake.routes["POST /api/admin/me/logout"] = () => json({ redirect: `${BASE}/admin/login` });
    mountAdmin("/admin");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(hard).toHaveBeenCalledWith(`${BASE}/admin/login`));
  });

  it("shows when the session ends, and hides 'Back to WhyGraph' for a password account", async () => {
    signedInAdmin();
    mountAdmin("/admin");
    expect(await screen.findByTestId("session-ends")).toHaveTextContent(/^Session ends /);
    expect(screen.getByRole("link", { name: "Back to WhyGraph" })).toHaveAttribute("href", "/orgs");
    expect(screen.getByRole("link", { name: "Account" })).toHaveAttribute("href", "/admin/account");
  });

  it("hides 'Back to WhyGraph' for a password account", async () => {
    signedInAdmin();
    fake.routes["/api/admin/me"] = () => json({ ...ME, has_password: true, github_login: null });
    mountAdmin("/admin");
    await screen.findByTestId("session-ends");
    expect(screen.queryByRole("link", { name: "Back to WhyGraph" })).toBeNull();
  });
});

describe("admin shell: gates", () => {
  it("renders Not found in local mode", async () => {
    resetAdminFake({ mode: "local", host_kind: undefined, base_url: undefined, user: { uid: "u", display_name: "Local", role: "owner" } });
    mountAdmin("/admin");
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
    expect(fake.calls.some((c) => c.path === "/api/admin/me")).toBe(false);
  });

  it("shows the instance-admin grantor copy on a 403", async () => {
    fake.routes["/api/admin/me"] = () => json({ error: "no", code: "forbidden" }, 403);
    mountAdmin("/admin");
    expect(
      await screen.findByText("You don't have access to the admin panel. Another platform administrator can give you access."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/organization owner or admin/)).toBeNull();
  });

  it("a failure other than 401 or 403 shows a retryable error, without the shell", async () => {
    fake.routes["/api/admin/me"] = () => json({ error: "boom" }, 500);
    mountAdmin("/admin");
    expect(await screen.findByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull();
  });
});

describe("admin shell: navigation", () => {
  beforeEach(() => signedInAdmin());

  it("lists the modules that exist, Dashboard highlighted only on /admin", async () => {
    mountAdmin("/admin/users");
    await screen.findByRole("heading", { name: "Users" });
    const links = within(nav()).getAllByRole("link");
    expect(links.map((l) => l.textContent)).toEqual(
      expect.arrayContaining(["Dashboard", "Organizations", "Users", "Audit"]),
    );
    expect(within(nav()).queryByRole("link", { name: "Projects" })).toBeNull();
    expect(within(nav()).queryByRole("link", { name: "Configuration" })).toBeNull();
    expect(within(nav()).getByRole("link", { name: "Users" })).toHaveAttribute("data-status", "active");
    expect(within(nav()).getByRole("link", { name: "Dashboard" })).not.toHaveAttribute("data-status", "active");
  });

  it("highlights Dashboard on /admin", async () => {
    mountAdmin("/admin");
    await screen.findByRole("heading", { name: "Dashboard" });
    expect(within(nav()).getByRole("link", { name: "Dashboard" })).toHaveAttribute("data-status", "active");
  });

  it("opens the drawer from the header menu button", async () => {
    mountAdmin("/admin");
    await screen.findByRole("heading", { name: "Dashboard" });
    await userEvent.setup().click(screen.getByRole("button", { name: "Open navigation" }));
    const drawer = await screen.findByRole("dialog");
    expect(within(drawer).getByRole("link", { name: "Organizations" })).toBeInTheDocument();
  });

  it.each([
    ["/admin", "Dashboard", ["Administration"], "Dashboard · Administration · WhyGraph"],
    ["/admin/orgs", "Organizations", ["Administration", "Organizations"], "Organizations · Administration · WhyGraph"],
    ["/admin/users", "Users", ["Administration", "Users"], "Users · Administration · WhyGraph"],
    ["/admin/audit", "Audit log", ["Administration", "Audit log"], "Audit log · Administration · WhyGraph"],
    ["/admin/account", "Account", ["Administration", "Account"], "Account · Administration · WhyGraph"],
    ["/admin/projects", "Projects", ["Administration", "Projects"], "Projects · Administration · WhyGraph"],
    [
      "/admin/config",
      "Platform configuration",
      ["Administration", "Platform configuration"],
      "Platform configuration · Administration · WhyGraph",
    ],
    ["/admin/orgs/acme", "Acme", ["Administration", "Organizations", "Acme"], "Acme · Administration · WhyGraph"],
    ["/admin/users/u2", "Ben", ["Administration", "Users", "Ben"], "Ben · Administration · WhyGraph"],
    [
      "/admin/projects/acme/api",
      "api",
      ["Administration", "Projects", "acme / api"],
      "api · Administration · WhyGraph",
    ],
  ])("%s has its h1, crumbs and title", async (path, h1, crumbs, title) => {
    mountAdmin(path);
    await screen.findByRole("heading", { level: 1, name: h1 });
    await waitFor(() => expect(document.title).toBe(title));
    expect(document.title).not.toMatch(/Not found/);
    const trail = screen.getByTestId("breadcrumbs");
    for (const c of crumbs) expect(within(trail).getByText(c)).toBeInTheDocument();
  });

  it("a module a later release builds says 'Not available yet', not 'this release'", async () => {
    mountAdmin("/admin/projects");
    expect(await screen.findByText("Not available yet")).toBeInTheDocument();
    expect(screen.getByText("This part of the admin panel comes in a later release.")).toBeInTheDocument();
    expect(screen.queryByText(/this release/i)).toBeNull();
  });

  it("an unknown address stays in the admin app and links back to the dashboard", async () => {
    mountAdmin("/admin/nope");
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to the dashboard" })).toHaveAttribute("href", "/admin");
    expect(within(nav()).getByRole("link", { name: "Users" })).toBeInTheDocument();
  });

  it("the crumbs helper names a detail page by its slug", () => {
    expect(adminCrumbs("/admin/orgs/acme/").crumbs.map((c) => c.label)).toEqual(["Administration", "Organizations", "acme"]);
  });
});

describe("admin shell: command palette", () => {
  beforeEach(() => signedInAdmin());

  async function openWithKeys() {
    const user = userEvent.setup();
    await screen.findByRole("heading", { name: "Dashboard" });
    await user.keyboard("{Meta>}k{/Meta}");
    return { user, box: await screen.findByRole("combobox") };
  }

  it("with an empty query shows only Go to, with the modules and Account", async () => {
    mountAdmin("/admin");
    await openWithKeys();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Go to")).toBeInTheDocument();
    for (const label of ["Dashboard", "Organizations", "Users", "Audit", "Account"]) {
      expect(within(dialog).getByText(label)).toBeInTheDocument();
    }
    expect(within(dialog).queryByText("Projects")).toBeNull();
    expect(within(dialog).queryByText("Users", { selector: "[cmdk-group-heading]" })).toBeNull();
    expect(screen.getByPlaceholderText("Go to a page, user, organization or project...")).toBeInTheDocument();
  });

  it("finds a user and opens their detail page", async () => {
    const router = mountAdmin("/admin");
    const { user, box } = await openWithKeys();
    await user.type(box, "ben");
    const dialog = screen.getByRole("dialog");
    await user.click(await within(dialog).findByText("@ben"));
    await waitFor(() => expect(router.state.location.pathname).toBe("/admin/users/u2"));
  });

  it("finds an organization by slug and opens its detail page", async () => {
    const router = mountAdmin("/admin");
    const { user, box } = await openWithKeys();
    await user.type(box, "acme");
    await user.click(await within(screen.getByRole("dialog")).findByText("Acme"));
    await waitFor(() => expect(router.state.location.pathname).toBe("/admin/orgs/acme"));
  });

  it("marks a deleted user and a deleted organization with a Deleted badge", async () => {
    fake.routes["/api/admin/users"] = () => json(USERS.map((u) => (u.uid === "u2" ? { ...u, deleted_at: "2026-11-10T00:00:00Z" } : u)));
    fake.routes["/api/admin/orgs"] = () => json(ORGS.map((o) => ({ ...o, deleted_at: "2026-11-10T00:00:00Z" })));
    mountAdmin("/admin");
    const { user, box } = await openWithKeys();
    await user.type(box, "b");
    const dialog = screen.getByRole("dialog");
    await within(dialog).findByText("@ben");
    expect(within(dialog).getAllByText("Deleted").length).toBeGreaterThanOrEqual(1);
    await user.clear(box);
    await user.type(box, "acme");
    await within(dialog).findByText("Acme");
    expect(within(dialog).getAllByText("Deleted").length).toBeGreaterThanOrEqual(1);
  });

  it("says 'No results.' for a query nothing matches", async () => {
    mountAdmin("/admin");
    const { user, box } = await openWithKeys();
    await user.type(box, "zzzz");
    expect(await within(screen.getByRole("dialog")).findByText("No results.")).toBeInTheDocument();
  });

  it("opens from the sidebar's Search button (the only way on a phone)", async () => {
    mountAdmin("/admin");
    await screen.findByRole("heading", { name: "Dashboard" });
    await userEvent.setup().click(within(nav()).getByRole("button", { name: /Search/ }));
    expect(await screen.findByRole("combobox")).toBeInTheDocument();
  });

  it("is closed when the admin app mounts, even if it was left open", async () => {
    mountAdmin("/admin");
    await screen.findByRole("heading", { name: "Dashboard" });
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("leaves 'g p' alone in the admin app", async () => {
    const router = mountAdmin("/admin/users");
    await screen.findByRole("heading", { name: "Users" });
    const user = userEvent.setup();
    await user.keyboard("gp");
    expect(router.state.location.pathname).toBe("/admin/users");
  });
});
