import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { AUDIT_EVENTS, AUDIT_GROUPS, auditEventsIn, auditLabel } from "../lib/auditEvents";

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
