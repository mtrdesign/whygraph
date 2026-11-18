import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ME, fake, json, mountAdmin, resetAdminFake, signedInAdmin, strays } from "./adminHarness";

beforeEach(() => {
  resetAdminFake();
  signedInAdmin();
});
afterEach(() => {
  expect(strays()).toEqual([]);
  vi.unstubAllGlobals();
});

const passwordAdmin = () =>
  (fake.routes["/api/admin/me"] = () => json({ ...ME, has_password: true, github_login: null, display_name: "Root" }));

describe("admin account page", () => {
  it("shows the session's start and end, and no password form for a GitHub admin", async () => {
    mountAdmin("/admin/account");
    const session = await screen.findByTestId("admin-session");
    expect(session).toHaveTextContent("Signed in");
    expect(session).toHaveTextContent("Ends");
    expect(screen.getByTestId("admin-identity")).toHaveTextContent("@ada");
    expect(screen.queryByRole("heading", { name: "Change password" })).toBeNull();
    expect(screen.queryByLabelText("Current password")).toBeNull();
  });

  it("posts {current, new} to the admin route for a password account", async () => {
    passwordAdmin();
    fake.routes["POST /api/admin/me/password"] = () => json({ changed: true });
    mountAdmin("/admin/account");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Current password"), "old password here!");
    await user.type(screen.getByLabelText("New password"), "a much longer new passphrase");
    await user.type(screen.getByLabelText("Confirm new password"), "a much longer new passphrase");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    await waitFor(() =>
      expect(fake.calls.find((c) => c.path === "/api/admin/me/password")?.body).toEqual({
        current: "old password here!",
        new: "a much longer new passphrase",
      }),
    );
    expect(fake.calls.some((c) => c.path === "/api/account/password")).toBe(false);
    await waitFor(() => expect(screen.getByLabelText("Current password")).toHaveValue(""));
  });

  it("holds the button until the two new passwords match", async () => {
    passwordAdmin();
    mountAdmin("/admin/account");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Current password"), "old");
    await user.type(screen.getByLabelText("New password"), "one");
    await user.type(screen.getByLabelText("Confirm new password"), "two");
    expect(screen.getByRole("button", { name: "Change password" })).toBeDisabled();
    expect(screen.getByText("The passwords do not match.")).toBeInTheDocument();
  });

  it("shows a wrong current password on its field", async () => {
    passwordAdmin();
    fake.routes["POST /api/admin/me/password"] = () => json({ error: "bad", code: "bad_credentials" }, 403);
    mountAdmin("/admin/account");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Current password"), "wrong");
    await user.type(screen.getByLabelText("New password"), "a much longer new passphrase");
    await user.type(screen.getByLabelText("Confirm new password"), "a much longer new passphrase");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText("That is not your current password.")).toBeInTheDocument();
  });
});
