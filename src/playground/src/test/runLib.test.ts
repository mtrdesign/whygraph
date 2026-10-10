import { describe, expect, it } from "vitest";
import { PROJECT_ACTIONS } from "../lib/permissions";
import { ApiError, type ProjectSummary, type ScanRunRow } from "../api";
import { mcpSnippet } from "../lib/agents";
import { projectProblem } from "../lib/errors";
import { projectStatus } from "../lib/projectStatus";
import {
  carriesCost,
  cancelledLabel,
  failureSummary,
  formatSeconds,
  requesterLabel,
  runLabel,
  runOutcome,
  runSeconds,
  runTitle,
  syncOutcome,
} from "../lib/scanFormat";
import { progressModel, rawPercent } from "../lib/scanProgress";
import { initialScanRunState, phaseRows, reduceScanRun, type ScanRunState } from "../lib/scanRun";

const fold = (events: Parameters<typeof reduceScanRun>[1][]): ScanRunState =>
  events.reduce(reduceScanRun, initialScanRunState);
const ev = (event: object) => ({ type: "event", event }) as Parameters<typeof reduceScanRun>[1];

describe("reduceScanRun access_revoked", () => {
  it("turns a cut stream into a failure, not a finished run", () => {
    const s = fold([
      ev({ type: "start", phase_total: 4 }),
      ev({ type: "end", run_id: 7, status: null, summary: null, reason: "access_revoked" }),
    ]);
    expect(s.finished).toBeNull();
    expect(s.failure).toEqual({
      status: 403,
      message: "You no longer have access to this project.",
      code: "access_revoked",
    });
  });
});

describe("phaseRows", () => {
  it("marks earlier phases done, the current one running, and later ones pending", () => {
    const s = fold([
      ev({ type: "start", phase_total: 4 }),
      ev({ type: "phase", phase: 1, title: "Structural crawl" }),
      ev({ type: "phase", phase: 2, title: "Author identity" }),
    ]);
    expect(phaseRows(s).map((p) => [p.title, p.status])).toEqual([
      ["Git history and GitHub", "done"],
      ["Author identities", "running"],
      ["Step 3", "pending"],
      ["Step 4", "pending"],
    ]);
  });

  it("names every phase up front from the start event's titles", () => {
    const s = fold([
      ev({ type: "start", phase_total: 4, phases: ["Structural crawl", "PR-origin recovery", "Author identity", "LLM descriptions"] }),
      ev({ type: "phase", phase: 1, title: "Structural crawl" }),
    ]);
    expect(phaseRows(s).map((p) => [p.title, p.status])).toEqual([
      ["Git history and GitHub", "running"],
      ["Pull request origins", "pending"],
      ["Author identities", "pending"],
      ["Commit descriptions", "pending"],
    ]);
  });

  it("skips the phases after a failure and marks a cancelled phase as stopped, not failed", () => {
    const phases = ["Structural crawl", "Author identity", "LLM descriptions"];
    const failed = fold([
      ev({ type: "start", phase_total: 3, phases }),
      ev({ type: "phase", phase: 1, title: "Structural crawl" }),
      ev({ type: "end", run_id: 1, status: "failed", summary: null }),
    ]);
    expect(phaseRows(failed).map((p) => p.status)).toEqual(["failed", "skipped", "skipped"]);
    const cancelled = fold([
      ev({ type: "start", phase_total: 3, phases }),
      ev({ type: "phase", phase: 1, title: "Structural crawl" }),
      ev({ type: "phase", phase: 2, title: "Author identity" }),
      ev({ type: "end", run_id: 1, status: "cancelled", summary: null }),
    ]);
    expect(phaseRows(cancelled).map((p) => p.status)).toEqual(["done", "cancelled", "skipped"]);
  });

  it("takes timings and crawler reports from the result, and flags a failed crawler's phase", () => {
    const s = fold([
      ev({ type: "start", phase_total: 2 }),
      ev({ type: "phase", phase: 1, title: "Structural crawl" }),
      ev({ type: "phase", phase: 2, title: "LLM descriptions" }),
      ev({
        type: "result",
        status: "failed",
        phase_timings: { "Structural crawl": 4.1, "LLM descriptions": 9 },
        crawlers: [
          { name: "git", status: "ok", summary: "240 commits" },
          { name: "analyze", status: "failed", error: "no key" },
        ],
      }),
      ev({ type: "end", run_id: 1, status: "failed", summary: null }),
    ]);
    const rows = phaseRows(s);
    expect(rows[0]).toMatchObject({ status: "done", seconds: 4.1 });
    expect(rows[0].crawlers[0].summary).toBe("240 commits");
    expect(rows[1]).toMatchObject({ status: "failed", seconds: 9 });
  });

  it("a run that died without a result fails the phase it was in; unreached phases are skipped", () => {
    const s = fold([
      ev({ type: "start", phase_total: 3 }),
      ev({ type: "phase", phase: 1, title: "Structural crawl" }),
      ev({ type: "end", run_id: 1, status: "interrupted", summary: null }),
    ]);
    expect(phaseRows(s).map((p) => p.status)).toEqual(["failed", "skipped", "skipped"]);
  });

  it("keeps a task's latest counter under its phase", () => {
    const s = fold([
      ev({ type: "start", phase_total: 1 }),
      ev({ type: "phase", phase: 1, title: "LLM descriptions" }),
      ev({ type: "task", name: "analyze", completed: 1, total: 62, description: "a" }),
      ev({ type: "task", name: "analyze", completed: 41, total: 62, description: "b" }),
    ]);
    expect(phaseRows(s)[0].tasks).toEqual([{ name: "analyze", completed: 41, total: 62, description: "b" }]);
  });
});

describe("scan formatting", () => {
  it("formats durations", () => {
    expect(formatSeconds(4.1)).toBe("4.1s");
    expect(formatSeconds(41.2)).toBe("41s");
    expect(formatSeconds(128)).toBe("2m 08s");
    expect(formatSeconds(3700)).toBe("1h 01m");
  });

  const row = (over: Partial<ScanRunRow>): ScanRunRow => ({
    id: 1,
    kind: "scan",
    trigger: "manual",
    analyze: true,
    status: "ok",
    requested_by: null,
    started_at: "2026-09-30T11:00:00+00:00",
    finished_at: "2026-09-30T11:00:30+00:00",
    summary: null,
    ...over,
  });

  it("prefers the child's elapsed time, falls back to wall time, and is null while running", () => {
    expect(runSeconds(row({ summary: { elapsed_sec: 12.5 } }))).toBe(12.5);
    expect(runSeconds(row({}))).toBe(30);
    expect(runSeconds(row({ finished_at: null }))).toBeNull();
  });

  it("names runs and titles them without an id", () => {
    expect(runLabel(row({ trigger: "initial" }))).toBe("First scan");
    expect(runLabel(row({ trigger: "initial", kind: "sync" }))).toBe("Import");
    expect(runLabel(row({ trigger: "manual", analyze: false }))).toBe("Quick rescan");
    expect(runLabel(row({ trigger: "manual", analyze: true }))).toBe("Full rescan");
    expect(runLabel(row({ trigger: "describe" }))).toBe("Describe");
    expect(runLabel(row({ trigger: "hook" }))).toBe("Commit hook");
    expect(runLabel(row({ trigger: "push", kind: "sync" }))).toBe("GitHub push");
    expect(runLabel(row({ trigger: "sync", kind: "sync" }))).toBe("Sync from GitHub");
    expect(runLabel(row({ trigger: "reconcile", kind: "sync" }))).toBe("Scheduled check");
    expect(runTitle(row({ queued_at: "2026-10-09T14:02:00+00:00" }))).toMatch(/^Full rescan - .*2026/);
    expect(runTitle(row({}))).toMatch(/^Full rescan - /);
    expect(runTitle(row({}))).not.toMatch(/#\d/);
  });

  it("says who asked and who cancelled, as 'You' only for the viewer", () => {
    expect(requesterLabel({ requested_by: null }, "u1")).toBe("System");
    expect(requesterLabel({ requested_by: { uid: "u1", label: "Ada" } }, "u1")).toBe("You");
    expect(requesterLabel({ requested_by: { uid: "u2", label: "Ben (@ben)" } }, "u1")).toBe("Ben (@ben)");
    const by = (uid: string) => ({ cancelled_by: { uid, label: "Ben" }, summary: { cancelled_by: "user" } });
    expect(cancelledLabel(by("u2"), "u1")).toBe("Cancelled by Ben");
    expect(cancelledLabel(by("u1"), "u1")).toBe("Cancelled by you");
    expect(cancelledLabel({ cancelled_by: null, summary: { cancelled_by: "budget" } }, "u1")).toBe(
      "Stopped: monthly budget reached",
    );
    expect(cancelledLabel({ cancelled_by: null, summary: null }, "u1")).toBe("Cancelled");
  });

  it("carries cost only when a row has the keys (absent, not null, is hidden)", () => {
    expect(carriesCost([row({})])).toBe(false);
    expect(carriesCost([row({}), row({ cost_usd: null })])).toBe(true);
  });

  it("words a sync's outcome, never 'No new commits' on a first run", () => {
    expect(syncOutcome({ trigger: "initial" }, { cloned: true, scanned: true })).toBe("Imported and scanned");
    expect(syncOutcome({ trigger: "push" }, { moved: true, scanned: true })).toBe("Fetched new commits and scanned");
    expect(syncOutcome({ trigger: "push" }, { moved: false })).toBe("No new commits");
    expect(syncOutcome({ trigger: "initial" }, { moved: false })).toBeNull();
  });

  it("summarises a failure through the registry, with the raw text kept for details", () => {
    const lost = failureSummary({ error: "the WhyGraph GitHub App cannot reach this repository any more - reconnect it on GitHub" });
    expect(lost.known).toBe(true);
    expect(lost.message).toContain("can no longer read this repository");
    expect(lost.details[0]).toContain("GitHub App");
    const key = failureSummary({ crawlers: [{ name: "analyze", status: "failed", error: "no API key for anthropic" }] });
    expect(key.settings).toBe("models");
    const odd = failureSummary({ error: "boom" });
    expect(odd.known).toBe(false);
    expect(odd.message).toContain("stopped before it finished");
    expect(odd.details).toEqual(["boom"]);
  });

  it("names outcomes for the history", () => {
    expect(runOutcome(row({ status: "failed", summary: { error: "boom" } }))).toBe("boom");
    expect(runOutcome(row({ status: "failed", summary: { exit_code: 2 } }))).toBe("Exit code 2");
    expect(runOutcome(row({ status: "cancelled", summary: { cancelled_by: "budget" } }))).toBe(
      "Stopped: monthly budget reached",
    );
    expect(runOutcome(row({ summary: { analyze_skipped: "budget" } }))).toBe(
      "LLM phase skipped: monthly budget reached",
    );
    expect(runOutcome(row({ status: "cancelled", summary: { merged_into: 9 } }))).toBe("Cancelled");
    expect(runOutcome(row({ summary: { merged_into: 9 } }))).toBe("Covered by another run");
    expect(runOutcome(row({ status: "cancelled", cancelled_by: { uid: "u1", label: "Ada" } }), "u1")).toBe(
      "Cancelled by you",
    );
    expect(runOutcome(row({ kind: "sync", summary: { moved: true } }))).toBe("Fetched new commits");
    expect(runOutcome(row({ kind: "sync", summary: { moved: true, scanned: true } }))).toBe(
      "Fetched new commits and scanned",
    );
    expect(runOutcome(row({ kind: "sync", summary: { moved: false } }))).toBe("No new commits");
    expect(runOutcome(row({ kind: "sync", trigger: "initial", summary: { cloned: true, scanned: true } }))).toBe(
      "Imported and scanned",
    );
    expect(runOutcome(row({ analyze: false, summary: { status: "ok" } }))).toBe("Structure only");
  });

  it("says a linked project's --codegraph-only run refreshed the code index (BUG-13)", () => {
    expect(runOutcome(row({ analyze: false, summary: { status: "ok", analyze_skipped: "--codegraph-only" } }))).toBe(
      "Code index refreshed",
    );
    // A missing key is still a skip of the descriptions.
    expect(runOutcome(row({ summary: { status: "ok", analyze_skipped: "no key for anthropic" } }))).toBe(
      "Descriptions skipped",
    );
  });
});

describe("reducer failure", () => {
  it("records a refused stream", () => {
    const s = reduceScanRun(initialScanRunState, {
      type: "failure",
      failure: { status: 404, message: "run 9 not found" },
    });
    expect(s.failure).toEqual({ status: 404, message: "run 9 not found" });
  });
});

describe("projectStatus with the last scan outcome", () => {
  const project = (over: Partial<ProjectSummary> = {}): ProjectSummary => ({
    slug: "a",
    name: "A",
    source: "local",
    root: "/a",
    remote_url: null,
    initialized: true,
    initialized_at: "x",
    last_scan_at: "x",
    created_at: "x",
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
    ...over,
  });

  it("shows a failed or interrupted last scan before staleness, and a running scan before both", () => {
    expect(projectStatus(project({ last_scan_status: "failed" }))).toMatchObject({ label: "Scan failed", tone: "error" });
    // An interrupted run is a failed scan in the pill's vocabulary (plan section 0.3 #38).
    expect(projectStatus(project({ last_scan_status: "interrupted", stale: { commits_behind: 2 } })).label).toBe(
      "Scan failed",
    );
    expect(
      projectStatus(project({ last_scan_status: "failed", running_scan: { id: 1, status: "running", trigger: "manual" } }))
        .label,
    ).toBe("Scanning");
    // A cancelled (merged) run is not a failure.
    expect(projectStatus(project({ last_scan_status: "cancelled" })).label).toBe("Ready");
  });
});

describe("projectProblem", () => {
  it("turns unsafe_path into an instruction that names the path", () => {
    const p = projectProblem(
      new ApiError(409, "refusing to use /r/.whygraph is a symbolic link", "unsafe_path", { path: "/r/.whygraph" }),
    );
    expect(p).toMatchObject({ kind: "unsafe_path", path: "/r/.whygraph" });
    expect(p.message).toContain("Replace the link with a real file or folder");
  });

  it("words other errors through the registry", () => {
    expect(projectProblem(new Error("nope"))).toMatchObject({ kind: "other", title: "Something went wrong" });
    expect(projectProblem(new Error("nope")).message).not.toContain("nope");
  });
});

describe("mcpSnippet", () => {
  const url = "http://127.0.0.1:8765/mcp/alpha";

  it("renders the same per-agent port forms as the files Initialize writes", () => {
    expect(JSON.parse(mcpSnippet("claude", url))).toEqual({
      mcpServers: { whygraph: { type: "http", url: "http://127.0.0.1:${WHYGRAPH_PORT:-8765}/mcp/alpha" } },
    });
    expect(JSON.parse(mcpSnippet("cursor", url))).toEqual({
      mcpServers: { whygraph: { url } },
    });
    expect(JSON.parse(mcpSnippet("vscode", url))).toEqual({
      inputs: [{ id: "whygraph-port", type: "promptString", description: "WhyGraph portal port", default: "8765" }],
      servers: { whygraph: { type: "http", url: "http://127.0.0.1:${input:whygraph-port}/mcp/alpha" } },
    });
    expect(mcpSnippet("codex", url)).toBe(`[mcp_servers.whygraph]\nurl = "${url}"\n`);
  });
});

// ---- the wizard's one progress bar (M2f-3 plan section 4.9) ------------------------------------

describe("reducer: plannedPhases, sync clone, maxPercent", () => {
  it("keeps the start event's titles apart from the phases seen so far", () => {
    const s = fold([
      ev({ type: "start", phase_total: 3, phases: ["Structural crawl", "Author identity", "LLM descriptions"] }),
      ev({ type: "phase", phase: 1, title: "Structural crawl" }),
    ]);
    expect(s.plannedPhases).toEqual(["Structural crawl", "Author identity", "LLM descriptions"]);
    expect(s.phases).toEqual([{ phase: 1, title: "Structural crawl" }]);
    // An older run's start has no titles.
    expect(fold([ev({ type: "start", phase_total: 2 })]).plannedPhases).toBeNull();
  });

  it("keeps the clone's repository name through the ok frame", () => {
    const s = fold([
      ev({ type: "sync", status: "cloning", full_name: "acme/api" }),
      ev({ type: "sync", status: "ok", cloned: true, moved: true }),
    ]);
    expect(s.sync).toMatchObject({ status: "ok", fullName: "acme/api", cloned: true });
  });

  it("never moves the bar backwards when a later task announces its total", () => {
    let s = fold([
      ev({ type: "start", phase_total: 1, phases: ["Structural crawl"] }),
      ev({ type: "phase", phase: 1, title: "Structural crawl" }),
      ev({ type: "task", name: "git", completed: 90, total: 100 }),
    ]);
    const high = s.maxPercent!;
    // 90% of the phases' 65 of 85 (CodeGraph's 20 is there from the start).
    expect(high).toBe(68);
    s = reduceScanRun(s, ev({ type: "task", name: "github", completed: 0, total: 100 }));
    // The pure ratio is now 45% of the phases (34% overall); the high-water mark stays.
    expect(rawPercent(s)).toBe(34);
    expect(s.maxPercent).toBe(high);
    expect(progressModel(s).percent).toBe(high);
    s = reduceScanRun(s, ev({ type: "end", run_id: 1, status: "ok", summary: null }));
    expect(progressModel(s).percent).toBe(100);
  });

  it("resets with the run", () => {
    const s = fold([
      ev({ type: "start", phase_total: 1 }),
      ev({ type: "phase", phase: 1, title: "Structural crawl" }),
      ev({ type: "task", name: "git", completed: 1, total: 2 }),
    ]);
    expect(s.maxPercent).toBe(38);
    expect(reduceScanRun(s, { type: "reset" }).maxPercent).toBeNull();
  });
});

describe("progressModel", () => {
  it("is indeterminate until any total is known", () => {
    const s = fold([ev({ type: "start", phase_total: 2 }), ev({ type: "phase", phase: 1, title: "Structural crawl" })]);
    expect(progressModel(s).percent).toBeNull();
    expect(progressModel(s).status).toBe("Reading git history");
  });

  it("splits the scan phases' share equally beside CodeGraph's fixed share from the start", () => {
    const base = [
      ev({ type: "start", phase_total: 2, phases: ["Structural crawl", "Author identity"] }),
      ev({ type: "phase", phase: 1, title: "Structural crawl" }),
      ev({ type: "task", name: "git", completed: 50, total: 100 }),
    ];
    // CodeGraph (20) beside the phases (65) before any codegraph event: half of
    // the first of two phases is 16.25 of 85, and the Code index row is pending.
    const early = progressModel(fold(base));
    expect(early.percent).toBe(19);
    expect(early.steps.at(-1)).toEqual({ label: "Code index", state: "pending" });
    // Its task showing changes nothing in the weights; the row runs.
    const withCg = fold([...base, ev({ type: "task", name: "codegraph", completed: 0, total: null })]);
    expect(rawPercent(withCg)).toBe(19);
    expect(progressModel(withCg).steps.at(-1)).toEqual({ label: "Code index", state: "running" });
    const cgDone = fold([...base, ev({ type: "task", name: "codegraph", completed: 1, total: 1 })]);
    expect(rawPercent(cgDone)).toBe(Math.floor(((20 + 16.25) / 85) * 100));
  });

  it("keeps the bar monotonic as the CodeGraph result lands", () => {
    let s = fold([
      ev({ type: "start", phase_total: 1, phases: ["Structural crawl"] }),
      ev({ type: "phase", phase: 1, title: "Structural crawl" }),
      ev({ type: "task", name: "git", completed: 10, total: 10 }),
    ]);
    const before = progressModel(s).percent!;
    s = reduceScanRun(s, ev({ type: "task", name: "codegraph", completed: 0, total: null }));
    expect(progressModel(s).percent).toBeGreaterThanOrEqual(before);
    s = reduceScanRun(s, ev({ type: "task", name: "codegraph", completed: 1, total: 1 }));
    expect(progressModel(s).percent).toBeGreaterThanOrEqual(before);
  });

  it("drops the CodeGraph share and row when the result lists crawlers without it", () => {
    const s = fold([
      ev({ type: "start", phase_total: 1, phases: ["Structural crawl"] }),
      ev({ type: "phase", phase: 1, title: "Structural crawl" }),
      ev({ type: "task", name: "git", completed: 5, total: 10 }),
      ev({ type: "result", status: "ok", crawlers: [{ name: "git", status: "ok" }] }),
    ]);
    expect(progressModel(s).steps.map((x) => x.label)).toEqual(["Git history and GitHub"]);
  });

  it("gives a production clone its share and names the repository", () => {
    const cloning = fold([ev({ type: "sync", status: "cloning", full_name: "acme/api" })]);
    expect(progressModel(cloning)).toMatchObject({ percent: null, status: "Cloning acme/api" });
    expect(progressModel(cloning).steps[0]).toEqual({ label: "Clone the repository", state: "running" });
    const scanning = fold([
      ev({ type: "sync", status: "cloning", full_name: "acme/api" }),
      ev({ type: "sync", status: "ok", cloned: true }),
      ev({ type: "start", phase_total: 1, phases: ["Structural crawl"] }),
      ev({ type: "phase", phase: 1, title: "Structural crawl" }),
      ev({ type: "task", name: "git", completed: 0, total: 100 }),
    ]);
    // The clone (15) of clone + CodeGraph + phases (100).
    expect(progressModel(scanning).percent).toBe(15);
    expect(progressModel(scanning).steps[0]).toEqual({ label: "Clone the repository", state: "done" });
    const fetching = fold([ev({ type: "sync", status: "fetching" })]);
    expect(progressModel(fetching, { fullName: "acme/api" }).status).toBe("Fetching acme/api from GitHub");
    expect(progressModel(fetching).steps[0].label).toBe("Fetch from GitHub");
  });

  it("treats a linked --codegraph-only run as one Code index step", () => {
    let s = fold([
      ev({ type: "start", phase_total: 1, phases: ["Code index"] }),
      ev({ type: "phase", phase: 1, title: "CodeGraph" }),
      ev({ type: "task", name: "codegraph", completed: 0, total: null }),
    ]);
    expect(progressModel(s)).toEqual({
      percent: null,
      status: "Building the code index",
      steps: [{ label: "Code index", state: "running" }],
    });
    s = reduceScanRun(s, ev({ type: "task", name: "codegraph", completed: 1, total: 1 }));
    expect(progressModel(s).percent).toBe(99);
    s = reduceScanRun(s, ev({ type: "end", run_id: 1, status: "ok", summary: null }));
    expect(progressModel(s)).toMatchObject({ percent: 100, status: "Done", steps: [{ label: "Code index", state: "done" }] });
  });

  it("writes the status line from the running task", () => {
    const at = (title: string, task: object) =>
      progressModel(
        fold([
          ev({ type: "start", phase_total: 4 }),
          ev({ type: "phase", phase: 1, title }),
          ev({ type: "task", ...task }),
        ]),
      ).status;
    expect(at("Structural crawl", { name: "git", completed: 1240, total: 5300 })).toBe(
      "Reading git history - 1,240 of 5,300 commits",
    );
    expect(at("Structural crawl", { name: "github", completed: 40, total: 120 })).toBe(
      "Fetching pull requests and issues - 40 of 120",
    );
    expect(at("LLM descriptions", { name: "analyze", completed: 12, total: 300 })).toBe(
      "Describing commits - 12 of 300 commits",
    );
  });

  it("lists the planned phases with human labels, and the failed one", () => {
    const s = fold([
      ev({ type: "start", phase_total: 3, phases: ["Structural crawl", "Author identity", "LLM descriptions"] }),
      ev({ type: "phase", phase: 1, title: "Structural crawl" }),
      ev({ type: "phase", phase: 2, title: "Author identity" }),
      ev({ type: "end", run_id: 1, status: "failed", summary: null }),
    ]);
    const model = progressModel(s);
    expect(model.status).toBe("The scan failed");
    expect(model.steps).toEqual([
      { label: "Git history and GitHub", state: "done" },
      { label: "Author identities", state: "failed" },
      { label: "Commit descriptions", state: "skipped" },
      { label: "Code index", state: "skipped" },
    ]);
  });
});
