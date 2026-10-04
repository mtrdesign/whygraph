import { execFileSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { env } from "./env";
import { themeRepos } from "./lib/fixtures";

function git(repo: string, ...args: string[]): void {
  execFileSync("git", ["-C", repo, ...args], { stdio: "pipe" });
}

/** A git repo with two commits, created fresh (any earlier copy is removed). */
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
}

/**
 * Creates the fixture repos under the temp root and refuses to run against a
 * portal that already has a user: the `setup` spec drives the first-run screen,
 * so the portal must be fresh (`make e2e` always starts one).
 */
export default async function globalSetup(): Promise<void> {
  const res = await fetch(`${env.baseUrl}/api/portal/state`, { headers: { "X-WhyGraph-Client": "1" } });
  if (!res.ok) throw new Error(`the portal at ${env.baseUrl} answered ${res.status} - is it running?`);
  const state = (await res.json()) as { setup_complete?: boolean };
  if (state.setup_complete) {
    throw new Error("the portal is already set up; start a fresh one (`make e2e` does)");
  }

  // The production portal must be unclaimed too. fetch() ignores a Host override
  // (and Node resolves *.localhost inconsistently), so go to the loopback address
  // with node:http and the Host the guard expects.
  const prod = new URL(env.prodUrl);
  const pres = await new Promise<{ status: number; body: string }>((resolve, reject) => {
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
  if (pres.status !== 200) throw new Error(`the production portal at ${env.prodUrl} answered ${pres.status}`);
  const pstate = JSON.parse(pres.body) as { mode?: string; bootstrap_required?: boolean };
  if (pstate.mode !== "production" || !pstate.bootstrap_required) {
    throw new Error("the production portal is not fresh; start a new one (`make e2e` does)");
  }

  fs.mkdirSync(env.control, { recursive: true });
  fs.rmSync(path.join(env.control, "fail"), { force: true });
  for (const theme of ["light", "dark"] as const) {
    for (const fx of Object.values(themeRepos(theme))) makeRepo(fx.path);
  }
  makeRepo(env.outside);
}
