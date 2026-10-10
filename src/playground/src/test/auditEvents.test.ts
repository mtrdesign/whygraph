import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { AUDIT_EVENTS, AUDIT_GROUPS, auditDetails, auditEventsIn, auditLabel, auditTarget } from "../lib/auditEvents";
import type { AuditEventRow } from "../api";

// Vitest runs from src/playground; the docs are two levels up.
const DOCS = resolve(process.cwd(), "../../docs/deploy/production.md");

/** The event names in the first column of the "security event log" table. */
function documentedEvents(): string[] {
  const text = readFileSync(DOCS, "utf-8");
  const section = text.slice(text.indexOf("## The security event log"));
  const table = section.slice(0, section.indexOf("Each carries the event"));
  const events: string[] = [];
  for (const line of table.split("\n")) {
    if (!line.startsWith("| `")) continue;
    const firstCell = line.split("|")[1] ?? "";
    for (const m of firstCell.matchAll(/`([a-z_]+)`/g)) events.push(m[1]);
  }
  return events;
}

describe("audit event labels", () => {
  it("labels every event in the docs' security event table", () => {
    const events = documentedEvents();
    expect(events.length).toBeGreaterThan(30);
    expect(events).toContain("key_tested");
    expect(events.filter((e) => !(e in AUDIT_EVENTS))).toEqual([]);
  });

  it("puts every event in one of the five groups, and every group has events", () => {
    for (const info of Object.values(AUDIT_EVENTS)) expect(AUDIT_GROUPS).toContain(info.group);
    for (const group of AUDIT_GROUPS) expect(auditEventsIn(group).length).toBeGreaterThan(0);
  });

  it("gives labels without underscores, and words an unknown event from its name", () => {
    for (const info of Object.values(AUDIT_EVENTS)) expect(info.label).not.toMatch(/_|—/);
    expect(auditLabel("member_added")).toBe("Member added");
    expect(auditLabel("something_new")).toBe("Something new");
    expect(auditLabel("constructor")).toBe("Constructor");
  });
});

describe("audit details (MEM-1)", () => {
  const row = (fields: Record<string, unknown>, over: Partial<AuditEventRow> = {}): AuditEventRow => ({
    id: 1,
    created_at: "2026-10-05T10:00:00Z",
    org: "acme",
    actor: null,
    event: "x",
    target: null,
    ip: null,
    fields,
    ...over,
  });
  const value = (fields: Record<string, unknown>) => auditDetails(row(fields))[0]?.value;

  it("words every reason word the backend audits", () => {
    // Literal reasons from src/whygraph/portal (`refused("...")`, `reason="..."`, runner REASON_*).
    const words = [
      "oauth_state", "unavailable", "2fa_required", "disabled", "password_change", "bad_login",
      "owner_required", "throttled", "no_such_github_user", "github_rate_limited", "github_unavailable",
      "no_such_project", "user_disabled", "already_member", "already_invited", "unknown_code", "pkce",
      "redirect_uri", "project_deleted", "member_removed", "project_access_removed", "user_revoked",
      "admin_revoked", "removed_locally", "no_access", "git_access_denied", "repo_deleted",
      "tracked_whygraph_state", "missing_signature", "bad_signature",
    ];
    for (const word of words) expect(value({ reason: word })).not.toMatch(/_/);
    expect(value({ reason: "something_new" })).toBe("Something new");
  });

  it("words results, providers, scopes and roles; formats money and rates", () => {
    expect(value({ result: "rate_limited" })).toBe("Rate limited");
    expect(value({ provider: "openai" })).toBe("OpenAI");
    expect(value({ scope: "member_default" })).toBe("Every member");
    expect(value({ role: "contributor" })).toBe("Contributor");
    expect(value({ monthly_usd: "25.00" })).toBe("$25");
    expect(value({ output_per_mtok: "15.000000" })).toBe("$15.00 per million tokens");
    expect(value({ threshold: 80 })).toBe("80%");
    expect(auditDetails(row({ github_login: "ada" }))[0]).toMatchObject({ label: "GitHub login", value: "@ada" });
  });

  it("shortens ids, keeps the whole one in the title, and drops empty fields", () => {
    const id = "76b37542-8a8a-4c1e-9d2b-0f1e2d3c4b5a";
    expect(auditDetails(row({ token_uid: id }))[0]).toEqual({ label: "Connection", value: "76b37542", title: id, mono: true });
    expect(auditDetails(row({ email: "", previous: null, invitation: undefined }))).toEqual([]);
    expect(auditTarget(row({}, { target: id }))).toMatchObject({ value: "76b37542", title: id });
    expect(auditTarget(row({}, { target: id, target_label: "Meg (@meg)" }))).toEqual({ value: "Meg (@meg)" });
    expect(auditTarget(row({}))).toBeNull();
  });
});
