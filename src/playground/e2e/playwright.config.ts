import { defineConfig } from "@playwright/test";
import { env } from "./env";

// Local-only browser suite (plan step 13b). `make e2e` (see run.sh) starts a
// real portal on a temp data dir, with the fake scanner, and exports the
// variables read in env.ts; it is not wired into CI. The spec files share that
// one portal, so they run one at a time, in order.
//
// `setup` creates the portal user through the UI once; `light` and `dark` then
// run the same flows under each colour scheme, each on its own pair of fixture
// repos so the second pass never collides with the first one's projects.
const browser = process.env.E2E_CHANNEL
  ? { channel: process.env.E2E_CHANNEL } // e.g. E2E_CHANNEL=chrome: the locally installed Chrome
  : {};

export default defineConfig({
  testDir: "./tests",
  outputDir: "./.artifacts/results",
  globalSetup: "./global-setup.ts",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "./.artifacts/report" }]],
  use: {
    baseURL: env.baseUrl,
    viewport: { width: 1280, height: 900 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...browser,
  },
  projects: [
    { name: "setup", testMatch: /setup\.spec\.ts/, use: { colorScheme: "light" } },
    {
      name: "light",
      testIgnore: /setup\.spec\.ts/,
      dependencies: ["setup"],
      use: { colorScheme: "light" },
    },
    {
      name: "dark",
      testIgnore: /setup\.spec\.ts/,
      dependencies: ["light"],
      use: { colorScheme: "dark" },
    },
  ],
});
