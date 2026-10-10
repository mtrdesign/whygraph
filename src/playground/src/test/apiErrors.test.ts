import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, portalApi, projectApi, setErrorMode } from "../api";
import { MESSAGES, SYNTHETIC_CODES, errorInfo, errorMessage, hasMessage } from "../lib/apiErrors";
import { authMessage } from "../lib/authErrors";
import { addProjectError, linkError, linkErrorMessage, projectProblem } from "../lib/errors";
import codes from "../lib/api-error-codes.json";

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });

afterEach(() => {
  setErrorMode("local");
  vi.unstubAllGlobals();
});

// ---- the inventory --------------------------------------------------------------------

describe("the registry and the backend's code inventory", () => {
  it("has an entry for every code the backend returns", () => {
    const missing = (codes as string[]).filter((code) => !hasMessage(code));
    expect(missing).toEqual([]);
  });

  it("has no entry the backend never returns (apart from the synthetic ones)", () => {
    const dead = Object.keys(MESSAGES).filter((code) => !(codes as string[]).includes(code) && !SYNTHETIC_CODES.includes(code));
    expect(dead).toEqual([]);
  });

  it("words every code as a sentence with plain hyphens, never the server's text", () => {
    for (const code of [...(codes as string[]), ...SYNTHETIC_CODES]) {
      for (const ctx of ["generic", "auth", "add-project", "github", "link", "chat", "usage"] as const) {
        const info = errorInfo(new ApiError(400, "raw server words", code), ctx);
        expect(info.message, `${code} (${ctx})`).not.toBe("raw server words");
        expect(info.message, code).not.toMatch(/—/);
        expect(info.message, code).not.toMatch(/\b[a-z]+_[a-z_]+\b/);
        expect(info.code).toBe(code);
        expect(info.detail).toBe("raw server words");
      }
    }
  });

  it("dropped the codes no backend emits", () => {
    expect(hasMessage("no_such_user")).toBe(false);
    expect(hasMessage("invalid_url")).toBe(false);
  });
});

// ---- context, extras and mode -----------------------------------------------------------

describe("context variants", () => {
  const err = (code: string) => new ApiError(400, "x", code);

  it("bad_token is a reset link under auth and a GitHub token elsewhere", () => {
    expect(errorMessage(err("bad_token"), "auth")).toMatch(/reset link/);
    expect(errorMessage(err("bad_token"), "add-project")).toMatch(/GitHub rejected this token/);
    expect(errorMessage(err("bad_token"), "github")).toMatch(/GitHub rejected this token/);
  });

  it("not_found names a repository when adding a project", () => {
    expect(errorMessage(err("not_found"), "add-project")).toMatch(/can't find that repository/);
    expect(errorMessage(err("not_found"))).toBe("That no longer exists.");
  });

  it("no_access tells a token from an installation", () => {
    expect(errorMessage(err("no_access"), "add-project")).toMatch(/This token cannot read/);
    expect(errorMessage(err("no_access"), "github")).toMatch(/installation/);
  });

  it("busy depends on where it came from", () => {
    expect(errorMessage(err("busy"), "link")).toMatch(/platform is busy/);
    expect(errorMessage(err("busy"), "github")).toMatch(/Another import/);
    expect(errorMessage(err("busy"))).toMatch(/A sync is finishing/);
  });

  it("bad_provider is the price form's under usage and the chat's elsewhere", () => {
    expect(errorMessage(err("bad_provider"), "usage")).toBe("Pick one of the listed providers.");
    expect(errorMessage(err("bad_provider"), "chat")).toBe("That provider isn't available here.");
  });
});

describe("extras", () => {
  it("names a budget refusal's scope", () => {
    const info = errorInfo(new ApiError(403, "x", "budget_exceeded", { scope: "member" }));
    expect(info.tone).toBe("warn");
    expect(info.message).toMatch(/your/i);
  });

  it("names a revocation's reason", () => {
    expect(errorMessage(new ApiError(401, "x", "token_revoked", { reason: "idle" }))).toContain("unused for too long");
    expect(errorMessage(new ApiError(401, "x", "token_revoked"))).toBe(
      "This connection was revoked. Link the project again to reconnect.",
    );
  });

  it("turns Retry-After into minutes", () => {
    expect(errorMessage(new ApiError(429, "x", "throttled", { retry_after: 600 }))).toBe(
      "Too many attempts. Try again in 10 minutes.",
    );
    expect(errorMessage(new ApiError(429, "x", "throttled", { retry_after: 30 }))).toBe(
      "Too many attempts. Try again in a minute.",
    );
    expect(errorMessage(new ApiError(429, "x", "throttled"))).toBe("Too many attempts. Try again in a few minutes.");
  });
});

describe("mode variants", () => {
  it("words forbidden for the mode", () => {
    expect(errorMessage(new ApiError(403, "your role (member) cannot x", "forbidden"))).toBe(
      "This action isn't available here.",
    );
    setErrorMode("production");
    expect(errorMessage(new ApiError(403, "your role (member) cannot x", "forbidden"))).toMatch(
      /An organization owner or admin can/,
    );
    expect(errorMessage(new ApiError(403, "your role on this project (viewer) cannot x", "forbidden"))).toMatch(
      /A project admin can/,
    );
  });

  it("words no_llm_key with the provider for the mode", () => {
    const err = new ApiError(0, "no API key is set for anthropic", "no_llm_key", { provider: "anthropic" });
    expect(errorMessage(err)).toBe("No Anthropic key. Add one in Settings > Models and keys.");
    setErrorMode("production");
    expect(errorMessage(err)).toBe("No Anthropic key. An owner can add one in Organization settings.");
  });

  it("words portal_unreachable for the mode", () => {
    const err = new ApiError(0, "Failed to fetch", "portal_unreachable");
    expect(errorMessage(err)).toMatch(/whygraph status/);
    setErrorMode("production");
    expect(errorMessage(err)).toBe("Can't reach WhyGraph. Check your connection.");
  });
});

// ---- fallbacks ------------------------------------------------------------------------

describe("codeless and unknown errors", () => {
  it("shows a codeless 4xx as the status title plus the server's sentence", () => {
    const info = errorInfo(new ApiError(409, "name must not be blank"));
    expect(info).toMatchObject({ code: "http_409", title: "Couldn't do that", message: "Name must not be blank." });
  });

  it("gives an unknown code the fixed sentence for its status, the server's text as detail", () => {
    const info = errorInfo(new ApiError(500, "boom", "weird"));
    expect(info.message).toMatch(/WhyGraph hit an error/);
    expect(info.detail).toBe("boom");
    expect(errorMessage(new ApiError(404, "gone", "constructor"))).toBe("That no longer exists.");
    expect(errorMessage(new ApiError(401, "x"))).toBe("Your session has ended. Sign in again.");
    expect(errorMessage(new ApiError(429, "slow"))).toMatch(/^Too many attempts/);
  });

  it("never shows a plain Error's text, only as detail", () => {
    const info = errorInfo(new Error("undefined is not a function"));
    expect(info.message).toMatch(/WhyGraph hit an error/);
    expect(info.detail).toBe("undefined is not a function");
  });
});

// ---- the transport --------------------------------------------------------------------

describe("parsing failures", () => {
  it("turns a 422 validation detail into invalid_request with fields", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json(
          {
            error: "invalid request",
            detail: [{ loc: ["body", "display_name"], msg: "Field required", type: "missing" }],
          },
          422,
        ),
      ),
    );
    const err = (await portalApi.setup("").catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("invalid_request");
    expect(err.message).toBe("display_name: Field required");
    expect(err.fields).toEqual([{ path: "display_name", msg: "Field required" }]);
    expect(errorMessage(err)).toBe("Some fields aren't valid: display_name: Field required.");
  });

  it("keeps a string detail, else error, else the status text", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "from detail" }, 400)));
    expect(((await portalApi.projects().catch((e) => e)) as ApiError).message).toBe("from detail");
    vi.stubGlobal("fetch", vi.fn(async () => json({ error: "from error", code: "not_git" }, 400)));
    const err = (await portalApi.projects().catch((e) => e)) as ApiError;
    expect([err.message, err.serverMessage, err.code]).toEqual(["from error", "from error", "not_git"]);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("<html>", { status: 502, statusText: "Bad Gateway" })),
    );
    expect(((await portalApi.projects().catch((e) => e)) as ApiError).message).toBe("Bad Gateway");
  });

  it("keeps Retry-After for throttled", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ error: "too many", code: "throttled" }, 429, { "Retry-After": "120" })),
    );
    const err = (await portalApi.projects().catch((e) => e)) as ApiError;
    expect(err.extra.retry_after).toBe(120);
    expect(errorMessage(err)).toBe("Too many attempts. Try again in 2 minutes.");
  });

  it("maps a network failure to portal_unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    const err = (await portalApi.projects().catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect([err.status, err.code]).toEqual([0, "portal_unreachable"]);
  });

  it("rethrows an AbortError unchanged", async () => {
    const abort = new DOMException("The operation was aborted.", "AbortError");
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(abort)));
    const err = await projectApi("a")
      .streamChat(1, "hi", () => {})
      .catch((e) => e);
    expect(err).toBe(abort);
  });
});

// ---- the wrappers -----------------------------------------------------------------------

describe("the thin wrappers", () => {
  it("authMessage reads the registry in the auth context", () => {
    expect(authMessage(new ApiError(400, "x", "bad_token"))).toMatch(/reset link/);
  });

  it("addProjectError puts the registry's sentence on the field that fixes it", () => {
    expect(addProjectError(new ApiError(400, "x", "not_git"), "local")).toEqual({
      field: "path",
      message: "This folder is not a git repository.",
    });
  });

  it("projectProblem keeps the unsafe_path instruction and path", () => {
    const p = projectProblem(new ApiError(409, "refusing", "unsafe_path", { path: "/r/.whygraph" }));
    expect(p).toMatchObject({ kind: "unsafe_path", path: "/r/.whygraph", title: "A symbolic link is in the way" });
  });

  it("linkErrorMessage is null for a code the registry does not know", () => {
    expect(linkErrorMessage(new ApiError(400, "x", "nope"))).toBeNull();
    expect(linkErrorMessage(new ApiError(409, "", "slug_taken"))).toMatch(/already exists on this machine/);
    expect(linkError(new ApiError(410, "x", "link_expired"))).toMatch(/expired/);
  });
});
