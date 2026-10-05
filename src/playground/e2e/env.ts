import path from "node:path";

/**
 * What `run.sh` exports for the suite. `root` is a temp dir outside the
 * checkout that holds `shared/` (the folder the portal shares, where the
 * fixture repos live), `outside/` (a repo the portal cannot see) and `control/`
 * (flag files read by the fake scanner).
 */
function need(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is not set - run the suite with \`make e2e\``);
  return value;
}

const root = need("WHYGRAPH_E2E_ROOT");

export const env = {
  baseUrl: need("WHYGRAPH_E2E_URL"),
  // The production-mode portal (`http://whygraph.localhost:<port>`) and its log,
  // where the one-time bootstrap secret is printed.
  prodUrl: need("WHYGRAPH_E2E_PROD_URL"),
  prodLog: need("WHYGRAPH_E2E_PROD_LOG"),
  // The fake GitHub (tests/github_fake.py) both production apps talk to; its
  // control routes (`POST /_fake/push`, ...) change state and send the webhook.
  githubUrl: need("WHYGRAPH_E2E_GITHUB_URL"),
  root,
  shared: path.join(root, "shared"),
  outside: path.join(root, "outside"),
  control: path.join(root, "control"),
};
