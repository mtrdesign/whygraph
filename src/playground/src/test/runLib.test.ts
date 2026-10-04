import { describe, expect, it } from "vitest";
import { ApiError, type ProjectSummary, type ScanRunRow } from "../api";
import { mcpSnippet } from "../lib/agents";
import { projectProblem } from "../lib/errors";
import { projectStatus } from "../lib/projectStatus";
import { formatSeconds, runOutcome, runSeconds, triggerLabel } from "../lib/scanFormat";
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
      ["Structural crawl", "done"],
      ["Author identity", "running"],
      ["Step 3", "pending"],
      ["Step 4", "pending"],
    ]);
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

  it("names triggers and outcomes", () => {
    expect(triggerLabel(row({ trigger: "hook" }))).toBe("Git hook");
    expect(triggerLabel(row({ trigger: "initial" }))).toBe("Initial");
    expect(triggerLabel(row({ trigger: "push" }))).toBe("Push");
    expect(triggerLabel(row({ trigger: "reconcile" }))).toBe("Reconcile");
    expect(runOutcome(row({ status: "failed", summary: { error: "boom" } }))).toBe("boom");
    expect(runOutcome(row({ status: "failed", summary: { exit_code: 2 } }))).toBe("Exit code 2");
    expect(runOutcome(row({ status: "cancelled", summary: { merged_into: 9 } }))).toBe("Merged into run #9");
    expect(runOutcome(row({ status: "cancelled", summary: { cancelled_by: "user" } }))).toBe("Cancelled by you");
    expect(runOutcome(row({ kind: "sync", summary: { moved: true } }))).toBe("Fetched new commits");
    expect(runOutcome(row({ analyze: false, summary: { status: "ok" } }))).toBe("Structure only");
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
    expect(projectStatus(project({ last_scan_status: "interrupted", stale: { commits_behind: 2 } })).label).toBe(
      "Scan interrupted",
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

  it("passes other errors through", () => {
    expect(projectProblem(new Error("nope"))).toMatchObject({ kind: "other", message: "nope" });
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
