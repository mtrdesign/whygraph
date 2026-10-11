import { defineConfig } from "@playwright/test";
import { env } from "../env";

// The scripted screenshot audit (M2f-3): not a test suite. It walks every screen
// of both portals that `e2e/run.sh` starts and writes full-height PNGs (light /
// dark x desktop / phone) plus manifest.json and gaps.md under $AUDIT_OUT
// (default: e2e/.artifacts/audit). Run it with
//
//   sh e2e/run.sh -c e2e/audit/playwright.config.ts
//
// (the last `-c` wins). See README.md.
const browser = process.env.E2E_CHANNEL ? { channel: process.env.E2E_CHANNEL } : {};

export default defineConfig({
  testDir: ".",
  testMatch: /\.audit\.ts$/,
  outputDir: "../.artifacts/audit-results",
  globalSetup: "./global-setup.ts",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 60 * 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    baseURL: env.baseUrl,
    viewport: { width: 1280, height: 900 },
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    trace: "off",
    screenshot: "off",
    ...browser,
  },
  projects: [
    { name: "local", testMatch: /local\.audit\.ts$/ },
    { name: "production", testMatch: /production\.audit\.ts$/, use: { baseURL: env.prodUrl } },
    // A linked project needs the local user (local) and the claimed platform (production).
    { name: "linked", testMatch: /linked\.audit\.ts$/, dependencies: ["local", "production"] },
  ],
});
