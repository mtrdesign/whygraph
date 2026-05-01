import { existsSync, statSync } from 'node:fs';
import { dirname, join, parse, resolve } from 'node:path';

export type RationaleBackend = 'api' | 'claude_cli';

export interface WhyGraphConfig {
  repoRoot: string;
  whyGraphDbPath: string;
  codeGraphDbPath: string | null;
  anthropicApiKey: string | undefined;
  model: string;
  evidenceTtlMs: number;
  rationaleBackend: RationaleBackend;
}

const DEFAULT_TTL_DAYS = 14;
const WHYGRAPH_DIR = '.whygraph';
const WHYGRAPH_DB_FILE = 'whygraph.db';

// Walk up from startPath looking for an existing .whygraph/whygraph.db.
// Mirrors findCodeGraphDb so a globally-installed MCP server can locate the
// per-project DB no matter which subdirectory Claude Code launches it from.
export function findWhyGraphDb(startPath: string): string | null {
  let current = resolve(startPath);
  const root = parse(current).root;
  while (true) {
    const dbPath = join(current, WHYGRAPH_DIR, WHYGRAPH_DB_FILE);
    if (existsSync(dbPath) && statSync(dbPath).isFile()) {
      return dbPath;
    }
    if (current === root) return null;
    const parent = dirname(current);
    if (parent === current) return null;
    current = parent;
  }
}

export function loadConfig(): WhyGraphConfig {
  const repoRoot = process.cwd();
  const ttlDaysRaw = Number.parseInt(
    process.env.WHYGRAPH_EVIDENCE_TTL_DAYS ?? '',
    10
  );
  const ttlDays = Number.isFinite(ttlDaysRaw) && ttlDaysRaw > 0 ? ttlDaysRaw : DEFAULT_TTL_DAYS;
  const whyGraphDbPath =
    process.env.WHYGRAPH_DB ??
    findWhyGraphDb(repoRoot) ??
    resolve(repoRoot, WHYGRAPH_DIR, WHYGRAPH_DB_FILE);
  return {
    repoRoot,
    whyGraphDbPath,
    codeGraphDbPath: process.env.CODEGRAPH_DB ?? null,
    anthropicApiKey: process.env.ANTHROPIC_API_KEY,
    model: process.env.WHYGRAPH_MODEL ?? 'claude-sonnet-4-6',
    evidenceTtlMs: ttlDays * 24 * 60 * 60 * 1000,
    rationaleBackend:
      process.env.WHYGRAPH_RATIONALE_BACKEND === 'claude_cli'
        ? 'claude_cli'
        : 'api',
  };
}
