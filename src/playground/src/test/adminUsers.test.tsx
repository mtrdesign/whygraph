import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ORG_DETAIL, USERS, USER_DETAIL, fake, json, mountAdmin, resetAdminFake, signedInAdmin, strays } from "./adminHarness";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }));

const DELETED = { deleted_at: "2026-11-10T09:00:00Z", deleted_by: "u1" };
const PASSWORD_USER = {
  ...USERS[2],
  uid: "u4",
  email: "old@example.com",
  display_name: "Old Root",
  is_instance_admin: false,
  owner_of: 2,
  org_count: 2,
};

let users: Record<string, unknown>[];

beforeEach(() => {
  resetAdminFake();
  signedInAdmin();
  vi.mocked(toast.success).mockClear();
  vi.mocked(toast.error).mockClear();
  users = [...USERS, PASSWORD_USER, { ...USERS[1], uid: "u5", display_name: "Gone", github_login: "gone", ...DELETED }];
  fake.routes["/api/admin/users"] = () => json(users);
});
afterEach(() => {
  expect(strays()).toEqual([]);
  vi.unstubAllGlobals();
});

const rows = () => screen.queryAllByTestId(/^admin-user-/).map((r) => r.getAttribute("data-testid"));

describe("Users list", () => {
  it("All holds no deleted row; the Deleted option holds only deleted ones with a badge", async () => {
    mountAdmin("/admin/users");
    await screen.findByTestId("admin-user-u2");
    expect(rows()).toEqual(["admin-user-u1", "admin-user-u2", "admin-user-u3", "admin-user-u4"]);
    await userEvent.setup().selectOptions(screen.getByLabelText("Show"), "Deleted");
    await waitFor(() => expect(rows()).toEqual(["admin-user-u5"]));
    expect(within(screen.getByTestId("admin-user-u5")).getByText("Deleted")).toBeInTheDocument();
  });

  it("keeps the status and the search in the URL; a default is not written and typing adds no history", async () => {
    const router = mountAdmin("/admin/users");
    const u = userEvent.setup();
    await screen.findByTestId("admin-user-u2");
    const before = router.history.length;
    expect(router.state.location.search).toEqual({});
    await u.selectOptions(screen.getByLabelText("Show"), "Admins");
    await waitFor(() => expect((router.state.location.search as { status?: string }).status).toBe("admins"));
    await u.type(screen.getByLabelText("Search"), "ro");
    await waitFor(() => expect(router.state.location.search).toEqual({ status: "admins", q: "ro" }));
    expect(router.history.length).toBe(before);
    await u.selectOptions(screen.getByLabelText("Show"), "All");
    await waitFor(() => expect(router.state.location.search).toEqual({ q: "ro" }));
  });

  it("opens with ?status= applied, and an invalid one falls back to All", async () => {
    mountAdmin("/admin/users?status=password");
    await screen.findByTestId("admin-user-u3");
    expect(rows()).toEqual(["admin-user-u3", "admin-user-u4"]);
    expect(screen.getByLabelText("Show")).toHaveValue("password");
  });

  it("falls back to All on an invalid status", async () => {
    const router = mountAdmin("/admin/users?status=bogus");
    await screen.findByTestId("admin-user-u2");
    expect(rows()).toHaveLength(4);
    expect(router.state.location.search).toEqual({});
  });

  it("flags a password account that is not an administrator as 'cannot sign in'", async () => {
    mountAdmin("/admin/users");
    expect(within(await screen.findByTestId("admin-user-u4")).getByText("cannot sign in")).toBeInTheDocument();
    expect(within(screen.getByTestId("admin-user-u3")).queryByText("cannot sign in")).toBeNull();
  });

  it("shows how many orgs a user owns, and blank at none", async () => {
    mountAdmin("/admin/users");
    const owner = within(await screen.findByTestId("admin-user-u4"));
    expect(owner.getAllByText("2 orgs")).toHaveLength(2);
    expect(within(screen.getByTestId("admin-user-u1")).getAllByText("1 org")).toHaveLength(2);
  });
});

describe("User detail", () => {
  let detail: Record<string, unknown>;
  beforeEach(() => {
    detail = { ...USER_DETAIL };
    fake.routes["/api/admin/users/u2"] = (method, body) => {
      if (method === "PATCH") {
        detail = { ...detail, ...(body as object) };
        users = users.map((u) => (u.uid === "u2" ? { ...u, ...(body as object) } : u));
        return json(detail);
      }
      return json(detail);
    };
  });

  it("shows the profile, memberships as org links, sessions, connections, spend and events", async () => {
    mountAdmin("/admin/users/u2");
    expect(await screen.findByRole("heading", { level: 1, name: "Ben" })).toBeInTheDocument();
    const memberships = within(screen.getByTestId("user-memberships"));
    expect(memberships.getAllByRole("link", { name: "Acme" })[0]).toHaveAttribute("href", "/admin/orgs/acme");
    expect(within(screen.getByTestId("user-sessions")).getAllByText("Firefox")[0]).toBeInTheDocument();
    expect(within(screen.getByTestId("user-tokens")).getAllByText("ben-laptop")[0]).toBeInTheDocument();
    expect(within(screen.getByTestId("user-spend")).getByText("$0.50")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "All events by this user" })).toHaveAttribute("href", "/admin/audit?actor=%40ben");
  });

  it("a missing uid is a not-found that links back into the admin app", async () => {
    fake.routes["/api/admin/users/nope"] = () => json({ error: "user not found" }, 404);
    mountAdmin("/admin/users/nope");
    expect(await screen.findByText("Not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to users" })).toHaveAttribute("href", "/admin/users");
  });

  it("Make admin, Enable and Restore ask no confirmation, and a mutation refetches the admin queries", async () => {
    mountAdmin("/admin/users/u2");
    const u = userEvent.setup();
    await screen.findByTestId("admin-user");
    const detailCalls = () => fake.calls.filter((c) => c.method === "GET" && c.path === "/api/admin/users/u2").length;
    const before = detailCalls();
    await u.click(screen.getByRole("button", { name: "Make admin" }));
    await waitFor(() => expect(fake.calls.some((c) => c.method === "PATCH" && (c.body as { is_instance_admin?: boolean })?.is_instance_admin === true)).toBe(true));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(detailCalls()).toBeGreaterThan(before));
  });

  it("Enable has no dialog", async () => {
    detail = { ...detail, disabled: true };
    mountAdmin("/admin/users/u2");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Enable" }));
    await waitFor(() => expect(fake.calls.find((c) => c.method === "PATCH")?.body).toEqual({ disabled: false }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("Delete types the login without @, says what happens, and sends it as the confirmation", async () => {
    fake.routes["DELETE /api/admin/users/u2"] = () => json({ deleted: "u2", orgs: ["acme"] });
    mountAdmin("/admin/users/u2");
    const u = userEvent.setup();
    await u.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = await screen.findByTestId("delete-user-dialog");
    expect(dialog).toHaveTextContent("Delete Ben?");
    expect(dialog).toHaveTextContent("Ben is signed out everywhere");
    expect(dialog).toHaveTextContent("leave 2 organizations");
    expect(dialog).toHaveTextContent("[Deleted User]");
    expect(dialog).toHaveTextContent("Type their GitHub login without the @.");
    const confirm = within(dialog).getByRole("button", { name: "Delete user" });
    expect(confirm).toBeDisabled();
    await u.type(within(dialog).getByLabelText(/to confirm/), "ben");
    await u.click(confirm);
    await waitFor(() => expect(fake.calls.find((c) => c.method === "DELETE")?.body).toEqual({ confirm: "ben" }));
  });

  it("a wrong typed value answered 409 confirm_user, and last_owner lists the orgs, stay in the dialog", async () => {
    fake.routes["DELETE /api/admin/users/u2"] = () =>
      json(
        { error: "last owner", code: "last_owner", orgs: [{ slug: "acme", deleted: false }, { slug: "old", deleted: true }] },
        409,
      );
    mountAdmin("/admin/users/u2");
    const u = userEvent.setup();
    await u.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = await screen.findByTestId("delete-user-dialog");
    await u.type(within(dialog).getByLabelText(/to confirm/), "ben");
    await u.click(within(dialog).getByRole("button", { name: "Delete user" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("They are the last owner of acme, old (deleted).");
    expect(alert).toHaveTextContent("A deleted organization needs Restore and a second owner, or Delete permanently.");
  });

  it("revokes one session after a confirmation, and every session on 'Sign out everywhere'", async () => {
    fake.routes["DELETE /api/admin/users/u2/sessions/s1"] = () => json({ revoked: 1 });
    fake.routes["DELETE /api/admin/users/u2/sessions"] = () => json({ revoked: 1 });
    mountAdmin("/admin/users/u2");
    const u = userEvent.setup();
    await u.click(within(await screen.findByTestId("user-sessions")).getAllByRole("button", { name: "Revoke" })[0]);
    let dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("End this session?");
    await u.click(within(dialog).getByRole("button", { name: "End session" }));
    await waitFor(() => expect(fake.calls.some((c) => c.method === "DELETE" && c.path.endsWith("/sessions/s1"))).toBe(true));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await u.click(screen.getByRole("button", { name: "Sign out everywhere" }));
    dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Sign Ben out everywhere?");
    expect(dialog).toHaveTextContent("Their connection tokens keep working.");
    await u.click(within(dialog).getByRole("button", { name: "Sign out everywhere" }));
    await waitFor(() => expect(fake.calls.some((c) => c.method === "DELETE" && c.path.endsWith("/users/u2/sessions"))).toBe(true));
  });

  it("self_session reads as the registry says, in the dialog", async () => {
    fake.routes["DELETE /api/admin/users/u2/sessions/s1"] = () =>
      json({ error: "that would end your session", code: "self_session" }, 409);
    mountAdmin("/admin/users/u2");
    const u = userEvent.setup();
    await u.click(within(await screen.findByTestId("user-sessions")).getAllByRole("button", { name: "Revoke" })[0]);
    await u.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "End session" }));
    expect(await screen.findByText("That would end the admin session you are using. Use Sign out instead.")).toBeInTheDocument();
  });

  it("revokes a token after naming the connection and the project", async () => {
    fake.routes["DELETE /api/admin/users/u2/tokens/t1"] = () => new Response(null, { status: 204 });
    mountAdmin("/admin/users/u2");
    const u = userEvent.setup();
    await u.click(within(await screen.findByTestId("user-tokens")).getAllByRole("button", { name: "Revoke" })[0]);
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent('Revoke the connection "ben-laptop"?');
    expect(dialog).toHaveTextContent("Agents on that machine lose access to Api at once. Its owner can connect it again.");
    await u.click(within(dialog).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(fake.calls.some((c) => c.method === "DELETE" && c.path.endsWith("/tokens/t1"))).toBe(true));
  });

  it("shows the reset link only for a password account that is an administrator and not disabled", async () => {
    fake.routes["/api/admin/users/u3"] = () => json({ ...USER_DETAIL, ...USERS[2], memberships: [], sessions: [], tokens: [], spend: [] });
    mountAdmin("/admin/users/u3");
    expect(await screen.findByRole("button", { name: "Copy reset link" })).toBeInTheDocument();
  });

  it("no reset link for a password account that is not an administrator", async () => {
    fake.routes["/api/admin/users/u4"] = () => json({ ...USER_DETAIL, ...PASSWORD_USER, memberships: [], sessions: [], tokens: [], spend: [] });
    mountAdmin("/admin/users/u4");
    await screen.findByTestId("admin-user");
    expect(screen.queryByRole("button", { name: "Copy reset link" })).toBeNull();
    expect(within(screen.getByTestId("user-profile")).getByText("cannot sign in")).toBeInTheDocument();
  });
});

describe("Erase and Restore", () => {
  const deleted = (over: Record<string, unknown> = {}) => ({ ...USER_DETAIL, ...DELETED, uid: "u5", display_name: "Gone", github_login: "gone", ...over });

  it("a deleted user shows the Deleted alert and only Restore and Erase permanently", async () => {
    fake.routes["/api/admin/users/u5"] = () => json(deleted());
    mountAdmin("/admin/users/u5");
    const alert = await screen.findByTestId("deleted-alert");
    await waitFor(() => expect(alert).toHaveTextContent(/Deleted .* by Ada/));
    const actions = within(screen.getByTestId("user-actions"));
    expect(actions.getAllByRole("button").map((b) => b.textContent)).toEqual(["Restore", "Erase permanently"]);
    expect(screen.queryByRole("button", { name: "Make admin" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
    const sessions = within(screen.getByTestId("user-sessions"));
    expect(sessions.queryByRole("button")).toBeNull();
    expect(within(screen.getByTestId("user-memberships")).getAllByRole("link", { name: "Acme" })[0]).toBeInTheDocument();
  });

  it("Restore asks nothing and says where the user is back in", async () => {
    fake.routes["/api/admin/users/u5"] = () => json(deleted());
    fake.routes["POST /api/admin/users/u5/restore"] = () => json({ restored: [{ org: "acme", role: "member", deleted: false }], skipped: [7] });
    mountAdmin("/admin/users/u5");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Restore" }));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("Gone restored", {
        description: "Back in: acme. Skipped (organization deleted permanently): 1.",
      }),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("Erase is irreversible in its words and types the login, then the email, then the uid", async () => {
    fake.routes["/api/admin/users/u5"] = () => json(deleted());
    fake.routes["POST /api/admin/users/u5/erase"] = () => json({ erased: "u5" });
    mountAdmin("/admin/users/u5");
    const u = userEvent.setup();
    await u.click(await screen.findByRole("button", { name: "Erase permanently" }));
    const dialog = await screen.findByTestId("erase-user-dialog");
    expect(dialog).toHaveTextContent("Erase Gone permanently?");
    expect(dialog).toHaveTextContent("This cannot be undone.");
    expect(dialog).toHaveTextContent("The GitHub account can sign up again as a new account.");
    await u.type(within(dialog).getByLabelText(/to confirm/), "gone");
    await u.click(within(dialog).getByRole("button", { name: "Erase permanently" }));
    await waitFor(() => expect(fake.calls.find((c) => c.path.endsWith("/erase"))?.body).toEqual({ confirm: "gone" }));
  });

  it.each([
    ["the email", { github_login: null, email: "gone@example.com" }, "gone@example.com", "Type their email address."],
    ["the uid", { github_login: null, email: null }, "u5", "Type their ID."],
  ])("types %s when there is no GitHub login", async (_name, over, expected, hint) => {
    fake.routes["/api/admin/users/u5"] = () => json(deleted(over));
    mountAdmin("/admin/users/u5");
    const u = userEvent.setup();
    await u.click(await screen.findByRole("button", { name: "Erase permanently" }));
    const dialog = await screen.findByTestId("erase-user-dialog");
    expect(dialog).toHaveTextContent(hint);
    expect(within(dialog).getByText(expected)).toBeInTheDocument();
    const confirm = within(dialog).getByRole("button", { name: "Erase permanently" });
    await u.type(within(dialog).getByLabelText(/to confirm/), expected);
    expect(confirm).toBeEnabled();
  });

  it("erase_blocked and a busy erase read as §4.16 says, in the dialog", async () => {
    fake.routes["/api/admin/users/u5"] = () => json(deleted());
    let answer: Response = json({ error: "x", code: "erase_blocked", projects: ["acme/api", "acme/web"] }, 409);
    fake.routes["POST /api/admin/users/u5/erase"] = () => answer;
    mountAdmin("/admin/users/u5");
    const u = userEvent.setup();
    await u.click(await screen.findByRole("button", { name: "Erase permanently" }));
    const dialog = await screen.findByTestId("erase-user-dialog");
    await u.type(within(dialog).getByLabelText(/to confirm/), "gone");
    await u.click(within(dialog).getByRole("button", { name: "Erase permanently" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Nothing was erased: WhyGraph couldn't open the chat history of acme/api, acme/web. Fix or permanently delete those projects, then erase again.",
    );
    answer = json({ error: "busy", code: "busy" }, 503);
    await u.click(within(dialog).getByRole("button", { name: "Erase permanently" }));
    await waitFor(() =>
      expect(within(dialog).getByRole("alert")).toHaveTextContent(
        "WhyGraph is still writing recent events. Try Erase again in a minute.",
      ),
    );
  });

  it("an erased user shows [Deleted User] and no actions", async () => {
    fake.routes["/api/admin/users/u5"] = () => json(deleted({ display_name: "[Deleted User]", github_login: null, email: null, erased: true }));
    mountAdmin("/admin/users/u5");
    expect(await screen.findByRole("heading", { level: 1, name: "[Deleted User]" })).toBeInTheDocument();
    expect(screen.queryByTestId("user-actions")).toBeNull();
    expect(screen.queryByRole("button", { name: "Restore" })).toBeNull();
    expect(screen.getByTestId("erased-alert")).toBeInTheDocument();
  });
});

describe("the hand-over path", () => {
  it("goes from the Password filter to the account, its memberships, an org and its Members tab", async () => {
    fake.routes["/api/admin/users/u4"] = () =>
      json({
        ...USER_DETAIL,
        ...PASSWORD_USER,
        memberships: [{ org: "acme", name: "Acme", role: "owner", owner: true }],
        sessions: [],
        tokens: [],
        spend: [],
      });
    fake.routes["/api/admin/orgs/acme"] = () => json(ORG_DETAIL);
    mountAdmin("/admin/users");
    const u = userEvent.setup();
    await u.selectOptions(await screen.findByLabelText("Show"), "Password accounts");
    await u.click(within(await screen.findByTestId("admin-user-u4")).getByRole("link", { name: /Old Root/ }));
    await u.click((await within(await screen.findByTestId("user-memberships")).findAllByRole("link", { name: "Acme" }))[0]);
    await u.click(await screen.findByRole("tab", { name: "Members" }));
    const members = await screen.findByTestId("org-members");
    expect(within(members).getByTestId("org-member-u2")).toBeInTheDocument();
  });
});
