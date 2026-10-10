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
      testIgnore: /(setup|production|linked|phone)\.spec\.ts/,
      dependencies: ["setup"],
      use: { colorScheme: "light" },
    },
    {
      name: "dark",
      testIgnore: /(setup|production|linked|phone)\.spec\.ts/,
      dependencies: ["light"],
      use: { colorScheme: "dark" },
    },
    // Production mode: its own portal (`env.prodUrl`), claimed through the
    // logged bootstrap secret. Independent of the local-mode projects above.
    {
      name: "production",
      testMatch: /(^|\/)production\.spec\.ts/,
      use: { baseURL: env.prodUrl, colorScheme: "light" },
    },
    // The main routes of both portals at a phone's width (M2f-3): no horizontal
    // overflow and an h1 on each. After `light` (its projects) and `production`
    // (its claimed instance and the `comet` organization).
    {
      name: "phone",
      testMatch: /phone\.spec\.ts/,
      dependencies: ["light", "production"],
      use: { baseURL: env.baseUrl, viewport: { width: 390, height: 844 }, colorScheme: "light" },
    },
    // Usage & cost on the production portal (M2f-2): its own organization, after
    // the claimed instance. One project, so its member budget never collides.
    {
      name: "production-usage",
      testMatch: /usage-production\.spec\.ts/,
      dependencies: ["production"],
      use: { baseURL: env.prodUrl, colorScheme: "light" },
    },
    // Onboarding on the production portal (M2f-3): its own organization, after
    // the claimed instance (ben, dee and the fake GitHub's installation).
    {
      name: "production-onboarding",
      testMatch: /onboarding-production\.spec\.ts/,
      dependencies: ["production"],
      use: { baseURL: env.prodUrl, colorScheme: "light" },
    },
    // A project linked to the platform (M2e): it drives both portals, so its
    // baseURL is the local one and it opens the platform by absolute URL. It
    // needs the local portal's user (`setup`) and the claimed instance with
    // its organizations (`production`).
    {
      name: "linked",
      testMatch: /linked\.spec\.ts/,
      dependencies: ["setup", "production"],
      use: { baseURL: env.baseUrl, colorScheme: "light" },
    },
  ],
});
