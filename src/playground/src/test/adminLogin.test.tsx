import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BASE, fake, hard, json, mountAdmin, resetAdminFake } from "./adminHarness";

const AUTHORIZE = "https://github.com/login/oauth/authorize?client_id=x&state=s";

beforeEach(() => resetAdminFake());
afterEach(() => vi.unstubAllGlobals());

const adminCalls = () => fake.calls.filter((c) => c.path.startsWith("/api/admin/"));

describe("admin login page", () => {
  it("has its own title, heading and description", async () => {
    mountAdmin("/admin/login");
    expect(await screen.findByRole("heading", { level: 1, name: "Sign in to administration" })).toBeInTheDocument();
    expect(
      screen.getByText("For platform administrators. Use the GitHub account that was made an administrator."),
    ).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("Admin sign-in · WhyGraph"));
    expect(document.getElementById("main")).not.toBeNull();
  });

  it("the GitHub button starts the ADMIN flow with an absolute next, then leaves for GitHub", async () => {
    fake.routes["POST /api/auth/github/admin/start"] = () => json({ authorize_url: AUTHORIZE });
    const next = `${BASE}/admin/users`;
    mountAdmin(`/admin/login?next=${encodeURIComponent(next)}`);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Continue with GitHub" }));
    await waitFor(() => expect(hard).toHaveBeenCalledWith(AUTHORIZE));
    const start = fake.calls.find((c) => c.path === "/api/auth/github/admin/start");
    expect(start?.method).toBe("POST");
    expect(start?.body).toEqual({ next });
    expect(fake.calls.some((c) => c.path === "/api/auth/github/start")).toBe(false);
  });

  it("without a usable next, returns to the dashboard (absolute)", async () => {
    fake.routes["POST /api/auth/github/admin/start"] = () => json({ authorize_url: AUTHORIZE });
    mountAdmin(`/admin/login?next=${encodeURIComponent("https://evil.example/admin")}`);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Continue with GitHub" }));
    await waitFor(() => expect(hard).toHaveBeenCalled());
    expect(fake.calls.find((c) => c.path === "/api/auth/github/admin/start")?.body).toEqual({ next: `${BASE}/admin` });
  });

  it("keeps the password form behind a disclosure and posts to the admin route", async () => {
    fake.routes["POST /api/auth/admin/login"] = () => json({ redirect: `${BASE}/admin` });
    mountAdmin("/admin/login");
    const toggle = await screen.findByRole("button", { name: "Sign in with a password" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("Email")).toBeNull();
    const user = userEvent.setup();
    await user.click(toggle);
    await user.type(await screen.findByLabelText("Email"), "root@example.com");
    await user.type(screen.getByLabelText("Password"), "correct horse battery");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(hard).toHaveBeenCalledWith(`${BASE}/admin`));
    const login = fake.calls.find((c) => c.path === "/api/auth/admin/login");
    expect(login?.body).toEqual({ email: "root@example.com", password: "correct horse battery", next: `${BASE}/admin` });
    expect(fake.calls.some((c) => c.path === "/api/auth/login")).toBe(false);
  });

  it("shows bad credentials inline and does not navigate", async () => {
    fake.routes["POST /api/auth/admin/login"] = () => json({ error: "bad", code: "bad_credentials" }, 401);
    mountAdmin("/admin/login");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Sign in with a password" }));
    await user.type(await screen.findByLabelText("Email"), "root@example.com");
    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByText("Incorrect email or password.");
    expect(hard).not.toHaveBeenCalled();
  });

  it("says so for a password account that is not an administrator", async () => {
    fake.routes["POST /api/auth/admin/login"] = () => json({ error: "no", code: "not_an_admin", surface: "admin" }, 403);
    mountAdmin("/admin/login");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Sign in with a password" }));
    await user.type(await screen.findByLabelText("Email"), "ben@example.com");
    await user.type(screen.getByLabelText("Password"), "correct horse battery");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByText(/This account is not a platform administrator/);
    expect(hard).not.toHaveBeenCalled();
  });

  it("says the session ended only with reauth=1", async () => {
    mountAdmin("/admin/login?next=" + encodeURIComponent(`${BASE}/admin`));
    await screen.findByRole("heading", { name: "Sign in to administration" });
    expect(screen.queryByText("Your admin session ended. Sign in again to continue.")).toBeNull();
    document.body.innerHTML = "";
    mountAdmin("/admin/login?next=" + encodeURIComponent(`${BASE}/admin`) + "&reauth=1");
    expect(await screen.findByText("Your admin session ended. Sign in again to continue.")).toBeInTheDocument();
  });

  it("calls no /api/admin route (it has no admin session to ask)", async () => {
    mountAdmin("/admin/login?reauth=1");
    await screen.findByRole("heading", { name: "Sign in to administration" });
    expect(adminCalls()).toEqual([]);
    expect(hard).not.toHaveBeenCalled();
  });
});
