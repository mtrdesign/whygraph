import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const SRC = resolve(import.meta.dirname, "..");
const EM_DASH = "—";

/** Wording the portal must not show (MODE-6). Matched case-insensitively. */
const BANNED = [/structural crawl/i, /folds? into the same run/i, /triggered is not benefited/i];

/**
 * Protocol identifiers that must equal what the scan child emits. They are matched
 * against events, never shown as written, so the banned phrase is allowed here.
 */
const PROTOCOL_FILES = new Set(["lib/scanProgress.ts", "lib/scanRun.ts"]);

function tsxFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "node_modules" ? [] : tsxFiles(path);
    return /\.tsx?$/.test(name) && !name.endsWith(".d.ts") ? [path] : [];
  });
}

/** Every string literal, template chunk and JSX text of a file (comments are not nodes). */
function texts(path: string): string[] {
  const source = ts.createSourceFile(path, readFileSync(path, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const found: string[] = [];
  const visit = (node: ts.Node) => {
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) found.push(node.text);
    else if (ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) found.push(node.text);
    else if (ts.isJsxText(node)) found.push(node.text);
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

describe("UI copy", () => {
  // The `.ts` modules hold copy too (labels, formatters), so they are walked as well as the `.tsx` files.
  const files = tsxFiles(SRC).filter((f) => !relative(SRC, f).startsWith("test/"));

  it("walks the sources", () => {
    expect(files.length).toBeGreaterThan(50);
  });

  it("has no em dash in a string literal, template literal or JSX text", () => {
    const hits = files.flatMap((f) =>
      texts(f)
        .filter((t) => t.includes(EM_DASH))
        .map((t) => `${relative(SRC, f)}: ${t.trim().slice(0, 80)}`),
    );
    expect(hits).toEqual([]);
  });

  it("uses none of the banned wording", () => {
    const hits = files
      .filter((f) => !PROTOCOL_FILES.has(relative(SRC, f)))
      .flatMap((f) =>
        texts(f)
          .filter((t) => BANNED.some((re) => re.test(t)))
          .map((t) => `${relative(SRC, f)}: ${t.trim().slice(0, 80)}`),
      );
    expect(hits).toEqual([]);
  });
});
