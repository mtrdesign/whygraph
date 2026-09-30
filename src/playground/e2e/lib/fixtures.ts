import path from "node:path";
import { env } from "../env";

export type Theme = "light" | "dark";

/** One fixture project: the folder the portal adds and what comes out of it. */
export interface Fixture {
  /** Folder name, which is also the project slug. */
  slug: string;
  /** Absolute path under the shared folder. */
  path: string;
  /** The display name the portal gives a newly added project (the folder name). */
  name: string;
  /** What the fake scanner writes into this repo's CodeGraph index: one file, two functions. */
  file: string;
  fileName: string;
  mainSymbol: string;
  qualifiedMain: string;
  helperSymbol: string;
}

function fixture(slug: string): Fixture {
  // Mirrors `seed_codegraph` in tests/fixtures/e2e_scan.py.
  const ident = slug.replace(/\W+/g, "_").toLowerCase();
  return {
    slug,
    path: path.join(env.shared, slug),
    name: slug,
    file: `src/${ident}.py`,
    fileName: `${ident}.py`,
    mainSymbol: `${ident}_main`,
    qualifiedMain: `${ident}.${ident}_main`,
    helperSymbol: `${ident}_helper`,
  };
}

/** The two repos each colour-scheme pass works on (never shared between passes). */
export function themeRepos(theme: Theme): { notes: Fixture; billing: Fixture } {
  return { notes: fixture(`notes-${theme}`), billing: fixture(`billing-${theme}`) };
}
