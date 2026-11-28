import { describe, expect, it } from "vitest";
import type { ProjectDetails } from "../api";
import { checkoutNotice } from "../lib/branchNotice";
import { projectHealth } from "../lib/projectHealth";
import { PROJECT_ACTIONS } from "../lib/permissions";

// Plan 64: the "scanned on an older branch" notice.

const behind = { current: "feature-x", default: "main", ahead: 1, behind: 120 };

describe("checkoutNotice", () => {
  it("names the branch, the count and what follows the checkout", () => {
    const n = checkoutNotice(behind);
    expect(n?.title).toBe("On feature-x, 120 commits behind main");
    expect(n?.detail).toContain("History includes all of main");
    expect(n?.detail).toContain("as of your last fetch");
  });

  it("says one commit, not one commits", () => {
    expect(checkoutNotice({ ...behind, behind: 1 })?.title).toBe("On feature-x, 1 commit behind main");
  });

  it("counts a detached HEAD as another branch", () => {
    expect(checkoutNotice({ ...behind, current: null })?.title).toBe("On a detached HEAD, 120 commits behind main");
  });

  it.each([
    ["not behind", { ...behind, behind: 0 }],
    ["behind unknown", { ...behind, behind: null }],
    ["on the default branch", { ...behind, current: "main" }],
    ["default unresolved", { current: "feature-x", default: null, ahead: null, behind: null }],
    ["no block", null],
    ["no block (undefined)", undefined],
  ])("is silent when %s", (_, b) => {
    expect(checkoutNotice(b)).toBeNull();
  });
});

function details(over: Partial<ProjectDetails> = {}): ProjectDetails {
  return {
    slug: "alpha",
    name: "Alpha",
    source: "local",
    root: "/repos/alpha",
    remote_url: null,
    initialized: true,
    initialized_at: "2026-01-01T00:00:00+00:00",
    last_scan_at: "2026-01-02T00:00:00+00:00",
    created_at: "2026-01-01T00:00:00+00:00",
    root_status: "ok",
    running_scan: null,
    restricted: false,
    my_role: "admin",
    permissions: PROJECT_ACTIONS,
    last_scan_status: "ok",
    stale: null,
    source_supported: true,
    access_lost: false,
    access_lost_reason: null,
    github_full_name: null,
    installation_account: null,
    last_scan_stats: { commits: 9, described_pct: 0, rationale_cards: 0, as_of: "2026-01-02T00:00:00+00:00", branch: behind },
    ...over,
  } as ProjectDetails;
}

describe("projectHealth checkout_branch item", () => {
  it("is an info item with no actions and leaves the pill alone", () => {
    const h = projectHealth(details());
    const item = h.items.find((i) => i.id === "checkout_branch");
    expect(item).toMatchObject({ tone: "info", actions: [], title: "On feature-x, 120 commits behind main" });
    expect(h.status.key).toBe("ready");
  });

  it("sits right after the behind item", () => {
    const ids = projectHealth(details({ stale: { commits_behind: 4 } })).items.map((i) => i.id);
    expect(ids.indexOf("checkout_branch")).toBe(ids.indexOf("behind") + 1);
  });

  it("is only for local, non-linked projects", () => {
    for (const source of ["github", "platform"] as const) {
      const ids = projectHealth(details({ source })).items.map((i) => i.id);
      expect(ids).not.toContain("checkout_branch");
    }
  });

  it("is absent on the default branch", () => {
    const ids = projectHealth(
      details({ last_scan_stats: { commits: 9, described_pct: 0, rationale_cards: 0, as_of: "x", branch: { ...behind, current: "main", behind: 0 } } }),
    ).items.map((i) => i.id);
    expect(ids).not.toContain("checkout_branch");
  });
});
