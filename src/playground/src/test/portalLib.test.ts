import { describe, expect, it, vi } from "vitest";
import { ApiError, projectApi, type ProjectSummary } from "../api";
import { addProjectError } from "../lib/errors";
import {
  CLAUDE_TOKEN,
  hooksToValue,
  layerToValues,
  secretsPatch,
  splitModel,
  valuesToLayer,
  configFormSchema,
} from "../lib/configForm";
import { initialScanRunState, reduceScanRun, phasePercent } from "../lib/scanRun";
import { projectStatus, timeAgo } from "../lib/projectStatus";
import { selectionNotes } from "../lib/agents";

// ---- add-project error mapping ------------------------------------------------

describe("addProjectError", () => {
  const err = (code: string, extra: Record<string, unknown> = {}) =>
    new ApiError(400, "backend text", code, extra);

  it("maps each access-probe code onto the field that fixes it", () => {
    expect(addProjectError(err("bad_token"), "github")).toMatchObject({ field: "token" });
    expect(addProjectError(err("bad_token"), "github").message).toMatch(/rejected/i);
    expect(addProjectError(err("no_access"), "github")).toMatchObject({ field: "token" });
    // GitHub answers 404 for both "missing" and "private, not visible to you".
    const nf = addProjectError(err("not_found"), "github");
    expect(nf.field).toBe("url");
    expect(nf.message).toMatch(/not visible/i);
    expect(addProjectError(err("invalid_url"), "github").field).toBe("url");
  });

  it("carries the not_shared fix command through from the body", () => {
    const e = addProjectError(
      err("not_shared", { command: "whygraph up --add-folder /Users/me/Work", folder_suggestion: "/Users/me/Work" }),
      "local",
    );
    expect(e.field).toBe("path");
    expect(e.command).toBe("whygraph up --add-folder /Users/me/Work");
    expect(e.folderSuggestion).toBe("/Users/me/Work");
  });

  it("falls back to the backend message for unknown codes and non-API errors", () => {
    expect(addProjectError(err("weird"), "local")).toEqual({ field: "form", message: "backend text" });
    expect(addProjectError(new Error("offline"), "local").message).toBe("offline");
  });
});

// ---- config form <-> layer ------------------------------------------------------

describe("config form mapping", () => {
  it("splits provider/model on the first slash only", () => {
    expect(splitModel("openrouter/anthropic/claude-x")).toEqual({
      provider: "openrouter",
      model: "anthropic/claude-x",
    });
    expect(splitModel("gpt-4o")).toEqual({ provider: "", model: "gpt-4o" });
    expect(splitModel("nope/gpt")).toEqual({ provider: "", model: "nope/gpt" });
  });

  const stored = {
    llm: { model: "anthropic/claude-sonnet", openai: { timeout_sec: 30 } },
    analyze: { model: "openai/gpt-4o", max_diff_chars: 9000 },
    scan: { forge: "auto", hooks: ["post-commit"], remote: "upstream" },
  };

  it("reads a stored layer, including a 1.x style provider/model task value", () => {
    const v = layerToValues(stored);
    expect(v.defaultModel).toEqual({ provider: "anthropic", model: "claude-sonnet" });
    expect(v.analyze).toEqual({ provider: "openai", model: "gpt-4o" });
    expect(v.forge).toBe(true);
    expect(v.hooks).toEqual({
      "post-commit": true,
      "post-merge": false,
      "post-rewrite": false,
      "post-checkout": false,
    });
  });

  it("saving an untouched form changes nothing (imported hooks list stays a list)", () => {
    const layer = valuesToLayer(stored, layerToValues(stored), { scan: true });
    // The analyze task is rewritten as explicit provider + model (same meaning)...
    expect(layer.analyze).toEqual({ provider: "openai", model: "gpt-4o", max_diff_chars: 9000 });
    // ...and every key the form does not own survives.
    expect(layer.scan).toEqual({ forge: "auto", hooks: ["post-commit"], remote: "upstream" });
    expect((layer.llm as Record<string, unknown>).openai).toEqual({ timeout_sec: 30 });
  });

  it("writes only what changed and prunes emptied tables", () => {
    const v = layerToValues(stored);
    v.defaultModel = { provider: "", model: "" };
    v.openaiBaseUrl = "https://gateway.example/v1";
    v.forge = false;
    v.hooks["post-merge"] = true;
    const layer = valuesToLayer(stored, v, { scan: true });
    const llm = layer.llm as Record<string, Record<string, unknown>>;
    expect(llm.model).toBeUndefined();
    expect(llm.openai).toEqual({ timeout_sec: 30, base_url: "https://gateway.example/v1" });
    expect((layer.scan as Record<string, unknown>).forge).toBe("off");
    expect((layer.scan as Record<string, unknown>).hooks).toEqual(["post-commit", "post-merge"]);
  });

  it("keeps a model id that contains a slash intact by writing provider and model separately", () => {
    const v = layerToValues({});
    v.chat = { provider: "openrouter", model: "anthropic/claude-x" };
    expect(valuesToLayer({}, v, { scan: false })).toEqual({
      chat: { provider: "openrouter", model: "anthropic/claude-x" },
    });
  });

  it("serializes hooks: all -> default, none -> false, some -> list", () => {
    const all = { "post-commit": true, "post-merge": true, "post-rewrite": true, "post-checkout": true };
    expect(hooksToValue(all)).toBeUndefined();
    expect(hooksToValue({ ...all, "post-commit": false, "post-merge": false, "post-rewrite": false, "post-checkout": false })).toBe(false);
    expect(hooksToValue({ ...all, "post-checkout": false })).toEqual(["post-commit", "post-merge", "post-rewrite"]);
  });

  it("secrets are write-only: typed sets, staged removal nulls, blank is absent", () => {
    const v = layerToValues({});
    expect(secretsPatch(v, [], { github: true })).toBeUndefined();
    v.keys.openai = " sk-new ";
    v.githubToken = "ghp_x";
    expect(secretsPatch(v, ["anthropic", "github"], { github: true })).toEqual({
      llm: { openai: "sk-new", anthropic: null },
      github_token: "ghp_x",
    });
    expect(secretsPatch(layerToValues({}), ["github"], { github: true })).toEqual({ github_token: null });
    expect(secretsPatch(layerToValues({}), ["github"], { github: false })).toBeUndefined();
  });

  it("the Claude subscription token is its own write-only secret, in either scope", () => {
    const v = layerToValues({});
    v.claudeToken = " sk-ant-oat01-new ";
    expect(secretsPatch(v, [], { github: false })).toEqual({ claude_oauth_token: "sk-ant-oat01-new" });
    expect(secretsPatch(layerToValues({}), [CLAUDE_TOKEN], { github: false })).toEqual({
      claude_oauth_token: null,
    });
  });

  it("validates: provider and model together, chat providers, urls", () => {
    const base = layerToValues({});
    expect(configFormSchema.safeParse(base).success).toBe(true);
    const half = { ...base, defaultModel: { provider: "openai", model: "" } };
    expect(configFormSchema.safeParse(half).success).toBe(false);
    const ollamaChat = { ...base, chat: { provider: "ollama", model: "" } };
    expect(configFormSchema.safeParse(ollamaChat).success).toBe(false);
    expect(configFormSchema.safeParse({ ...base, openaiBaseUrl: "not a url" }).success).toBe(false);
  });
});

// ---- scan run ---------------------------------------------------------------------

describe("scan run reducer", () => {
  it("folds start / phase / task / end events", () => {
    let s = initialScanRunState;
    s = reduceScanRun(s, { type: "event", event: { type: "start", phase_total: 4 } });
    s = reduceScanRun(s, { type: "event", event: { type: "phase", phase: 2, title: "Git history" } });
    s = reduceScanRun(s, {
      type: "event",
      event: { type: "task", name: "git", completed: 3, total: 10, description: "walking" },
    });
    s = reduceScanRun(s, {
      type: "event",
      event: { type: "task", name: "git", completed: 10, total: 10, description: "done" },
    });
    expect(s.phaseTotal).toBe(4);
    expect(s.phaseTitle).toBe("Git history");
    expect(s.tasks).toEqual([{ name: "git", completed: 10, total: 10, description: "done" }]);
    expect(phasePercent(s)).toBe(25);
    s = reduceScanRun(s, {
      type: "event",
      event: { type: "end", run_id: 1, status: "ok", summary: null },
    });
    expect(s.finished).toBe("ok");
    expect(phasePercent(s)).toBe(100);
  });
});

describe("streamScanEvents", () => {
  function sse(chunks: string[]) {
    const enc = new TextEncoder();
    return new Response(
      new ReadableStream({
        start(c) {
          for (const ch of chunks) c.enqueue(enc.encode(ch));
          c.close();
        },
      }),
      { status: 200, headers: { "content-type": "text/event-stream" } },
    );
  }

  it("parses frames split across chunks, skips heartbeats, tracks ids, sends the client header", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      sse([
        'id: 10\ndata: {"type":"start","phase_',
        'total":2}\n\n: heartbeat\n\nid: 25\ndata: {"type":"phase","phase":1,"title":"A"}\n\n',
        'id: 40\nevent: end\ndata: {"type":"end","run_id":7,"status":"ok","summary":null}\n\n',
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);
    const seen: string[] = [];
    const last = await projectApi("alpha").streamScanEvents(7, (e) => seen.push(e.type), {
      lastEventId: "5",
    });
    vi.unstubAllGlobals();

    expect(seen).toEqual(["start", "phase", "end"]);
    expect(last).toBe("40");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/projects/alpha/scans/7/events");
    expect(init.headers["X-WhyGraph-Client"]).toBe("1");
    expect(init.headers["Last-Event-ID"]).toBe("5");
  });

  it("rejects a non-2xx status with the backend error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: "run 9 not found" }), {
          status: 404,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    await expect(projectApi("alpha").streamScanEvents(9, () => {})).rejects.toMatchObject({
      status: 404,
      message: "run 9 not found",
    });
    vi.unstubAllGlobals();
  });
});

// ---- project status -----------------------------------------------------------------

function project(over: Partial<ProjectSummary> = {}): ProjectSummary {
  return {
    slug: "a",
    name: "A",
    source: "local",
    root: "/r/a",
    remote_url: null,
    initialized: true,
    initialized_at: "2026-01-01T00:00:00+00:00",
    last_scan_at: "2026-01-02T00:00:00+00:00",
    created_at: "2026-01-01T00:00:00+00:00",
    root_status: "ok",
    running_scan: null,
    stale: null,
    ...over,
  };
}

describe("projectStatus", () => {
  it("covers every badge, by precedence", () => {
    expect(projectStatus(project()).key).toBe("ready");
    expect(projectStatus(project({ initialized: false })).key).toBe("uninitialized");
    expect(projectStatus(project({ last_scan_at: null })).key).toBe("unscanned");
    expect(projectStatus(project({ running_scan: { id: 1, status: "running", trigger: "manual" } })).label).toBe("Scanning");
    expect(projectStatus(project({ stale: { commits_behind: 3 } })).label).toBe("Stale, 3 commits behind");
    expect(projectStatus(project({ stale: { commits_behind: 1 } })).label).toBe("Stale, 1 commit behind");
    expect(projectStatus(project({ stale: { commits_behind: null } })).label).toBe("Stale");
    expect(projectStatus(project({ root_status: "missing", stale: { commits_behind: 3 } })).key).toBe("unavailable");
  });

  it("formats relative times", () => {
    const now = Date.parse("2026-09-30T12:00:00Z");
    expect(timeAgo("2026-09-30T11:48:00Z", now)).toBe("12 min ago");
    expect(timeAgo("2026-09-30T11:59:40Z", now)).toBe("just now");
    expect(timeAgo(null, now)).toBeNull();
  });
});

describe("agent notes", () => {
  it("adds the double-registration note only for claude + vscode together", () => {
    expect(selectionNotes(["claude"]).some((n) => /twice/.test(n))).toBe(false);
    expect(selectionNotes(["claude", "vscode"]).some((n) => /twice/.test(n))).toBe(true);
    expect(selectionNotes(["codex"]).join(" ")).toMatch(/trust this project/);
  });
});
