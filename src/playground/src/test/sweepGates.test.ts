import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = resolve(import.meta.dirname, "..");

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "node_modules" || name === "test" ? [] : sources(path);
    return /\.tsx?$/.test(name) && !name.endsWith(".d.ts") ? [path] : [];
  });
}

const FILES = sources(SRC).map((path) => ({ rel: relative(SRC, path), lines: readFileSync(path, "utf8").split("\n") }));

/** `file:line: text` for every code line (comments skipped) matching `re`, outside `allowed` files. */
function hits(re: RegExp, allowed: string[] = []): string[] {
  return FILES.filter((f) => !allowed.includes(f.rel)).flatMap((f) =>
    f.lines.flatMap((line, i) => {
      const code = line.trim();
      if (code.startsWith("//") || code.startsWith("*") || code.startsWith("/*")) return [];
      return re.test(line) ? [`${f.rel}:${i + 1}: ${code.slice(0, 90)}`] : [];
    }),
  );
}

describe("consistency gates", () => {
  it("formats dates only through lib/format.ts (CN-3)", () => {
    expect(hits(/toLocaleDateString|toLocaleTimeString|new Date\([^)]*\)\.toLocaleString/, ["lib/format.ts"])).toEqual([]);
  });

  it("never shows 'Failed to load' (ER-1: ErrorState words the failure)", () => {
    expect(hits(/Failed to load/)).toEqual([]);
  });

  it("renders no raw error message outside the error registry (ER-2)", () => {
    // `err.message` / `error.message` / `query.error.message`. The allow-list holds the registry itself, the
    // components that render it, the transport (api.ts wraps a network error) and the scan-run reducer, which
    // stores the message for `ErrorState`. LocalSource's `error` is an already-worded `AddError`, not a thrown error.
    const allowed = [
      "api.ts",
      "lib/apiErrors.ts",
      "lib/scanRun.ts",
      "components/state/ErrorState.tsx",
      "components/state/PortalErrorPage.tsx",
      "components/portal/LocalSource.tsx",
    ];
    expect(hits(/\b(?:err|error|e)\??\.message\b|\.error\??\.message\b/, allowed)).toEqual([]);
  });
});
