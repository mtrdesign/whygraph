import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { env } from "../env";
import { GAP_LINES, MANIFEST_LINES, OUT, SHOTS } from "./lib/shoot";
import { AUDIT_LLM_PORT, LOCAL_REPOS } from "./lib/seed";

function git(repo: string, ...args: string[]): void {
  execFileSync("git", ["-C", repo, ...args], { stdio: "pipe" });
}

/** A git repo with a few commits, created fresh. */
function makeRepo(repo: string): void {
  fs.rmSync(repo, { recursive: true, force: true });
  fs.mkdirSync(repo, { recursive: true });
  git(repo, "init", "-q", "-b", "main");
  git(repo, "config", "user.email", "e2e@example.com");
  git(repo, "config", "user.name", "E2E");
  git(repo, "config", "commit.gpgsign", "false");
  fs.writeFileSync(path.join(repo, "app.py"), "print('hi')\n");
  git(repo, "add", "app.py");
  git(repo, "commit", "-q", "-m", "initial commit");
  fs.appendFileSync(path.join(repo, "app.py"), "print('bye')\n");
  git(repo, "commit", "-q", "-am", "second commit");
  fs.writeFileSync(path.join(repo, "README.md"), `# ${path.basename(repo)}\n`);
  git(repo, "add", "README.md");
  git(repo, "commit", "-q", "-m", "Add a README");
  // The file the fake scanner's CodeGraph index points at (tests/fixtures/e2e_scan.py:
  // main on lines 1-6, helper on 8-12), so blame, evidence and rationale have a target.
  const ident = path.basename(repo).replace(/\W+/g, "_").replace(/^_|_$/g, "").toLowerCase();
  fs.mkdirSync(path.join(repo, "src"), { recursive: true });
  const main = [`def ${ident}_main():`, `    """Entry point."""`, "    value = 1", "    value += 1", `    ${ident}_helper()`, "    return value", ""];
  const helper = [`def ${ident}_helper():`, `    """Does the work."""`, "    total = 0", "    total += 2", "    return total"];
  fs.writeFileSync(path.join(repo, "src", `${ident}.py`), [...main, ...helper.slice(0, 3), ""].join("\n"));
  git(repo, "add", "src");
  git(repo, "commit", "-q", "-m", `Add ${ident} module`);
  fs.writeFileSync(path.join(repo, "src", `${ident}.py`), [...main, ...helper].join("\n") + "\n");
  git(repo, "commit", "-q", "-am", `Finish the ${ident} helper`);
}

function prodState(): Promise<{ status: number; body: string }> {
  const prod = new URL(env.prodUrl);
  return new Promise((resolve, reject) => {
    http
      .get(
        { host: "127.0.0.1", port: prod.port, path: "/api/portal/state", headers: { "X-WhyGraph-Client": "1", Host: prod.host } },
        (res) => {
          let body = "";
          res.on("data", (c) => (body += c));
          res.on("end", () => resolve({ status: res.statusCode ?? 0, body }));
        },
      )
      .on("error", reject);
  });
}

async function waitFor(url: string, tries = 60): Promise<void> {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(url);
      if (r.ok) return;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`${url} never answered`);
}

/**
 * The audit's global setup: both portals must be fresh (the audit walks first-run
 * screens), the local fixture repos are (re)created, and the scripted LLM
 * (llm_script.py) is started. The returned teardown stops it and writes
 * manifest.json and gaps.md.
 */
export default async function globalSetup(): Promise<() => Promise<void>> {
  const res = await fetch(`${env.baseUrl}/api/portal/state`, { headers: { "X-WhyGraph-Client": "1" } });
  if (!res.ok) throw new Error(`the portal at ${env.baseUrl} answered ${res.status}`);
  if (((await res.json()) as { setup_complete?: boolean }).setup_complete) {
    throw new Error("the local portal is already set up; the audit needs a fresh one (run it through e2e/run.sh)");
  }
  const p = await prodState();
  const ps = JSON.parse(p.body) as { mode?: string; bootstrap_required?: boolean };
  if (p.status !== 200 || ps.mode !== "production" || !ps.bootstrap_required) {
    throw new Error("the production portal is not fresh; run the audit through e2e/run.sh");
  }

  fs.rmSync(SHOTS, { recursive: true, force: true });
  fs.rmSync(path.join(OUT, "failures"), { recursive: true, force: true });
  fs.rmSync(MANIFEST_LINES, { force: true });
  fs.rmSync(GAP_LINES, { force: true });
  fs.mkdirSync(SHOTS, { recursive: true });

  fs.mkdirSync(env.control, { recursive: true });
  for (const f of ["fail", "delay"]) fs.rmSync(path.join(env.control, f), { force: true });
  for (const slug of LOCAL_REPOS) makeRepo(path.join(env.shared, slug));
  // A github.com origin (never fetched) so the wizard's GitHub token row shows for notes.
  git(path.join(env.shared, "notes"), "remote", "add", "origin", "https://github.com/acme/notes.git");

  const llm = spawn("python3", [path.resolve("e2e/audit/llm_script.py"), "--host", "127.0.0.1", "--port", String(AUDIT_LLM_PORT)], {
    stdio: "ignore",
  });
  await waitFor(`http://127.0.0.1:${AUDIT_LLM_PORT}/v1/models`);

  return async () => {
    llm.kill();
    writeOutputs();
  };
}

function readLines<T>(file: string): T[] {
  if (!fs.existsSync(file)) return [];
  return fs
    .readFileSync(file, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((l) => JSON.parse(l) as T);
}

interface Entry {
  file: string;
  mode: string;
  area: string;
  page: string;
  state: string;
  console_errors?: string[];
  api_errors?: string[];
  overflow_x?: string[];
}
interface Gap {
  mode: string;
  area: string;
  page: string;
  state: string;
  reason: string;
}

/** manifest.json (every PNG) and gaps.md (static known gaps + what failed in this run). */
export function writeOutputs(): void {
  const shots = readLines<Entry>(MANIFEST_LINES);
  const gaps = readLines<Gap>(GAP_LINES);
  const counts: Record<string, Record<string, number>> = {};
  for (const s of shots) {
    counts[s.mode] ??= {};
    counts[s.mode][s.area] = (counts[s.mode][s.area] ?? 0) + 1;
  }
  fs.writeFileSync(
    path.join(OUT, "manifest.json"),
    JSON.stringify({ generated: new Date().toISOString(), root: OUT, total: shots.length, counts, shots }, null, 2) + "\n",
  );
  const known = fs.readFileSync(path.resolve("e2e/audit/known-gaps.md"), "utf8");
  const lines = [known.trimEnd(), "", "## Not captured in this run (a step failed)", ""];
  if (!gaps.length) lines.push("None.");
  for (const g of gaps) lines.push(`- **${g.mode} / ${g.area} / ${g.page}** (${g.state}): ${g.reason}`);
  const flagged = shots.filter((s) => s.console_errors || s.overflow_x || s.api_errors);
  lines.push("", "## Flagged while capturing", "", "Console errors, API answers >= 400 and phone-width horizontal overflow, per screenshot (expected ones included: the error variants fail requests on purpose).", "");
  if (!flagged.length) lines.push("None.");
  for (const s of flagged) {
    const bits = [
      s.console_errors && `console: ${s.console_errors.join(" | ")}`,
      s.api_errors && `api: ${s.api_errors.join(", ")}`,
      s.overflow_x && `overflow-x: ${s.overflow_x.join(", ")}`,
    ].filter(Boolean);
    lines.push(`- \`${s.file}\` - ${bits.join("; ")}`);
  }
  fs.writeFileSync(path.join(OUT, "gaps.md"), lines.join("\n") + "\n");
}
