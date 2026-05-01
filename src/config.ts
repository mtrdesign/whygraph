import { resolve } from 'node:path';

export interface WhyGraphConfig {
  repoRoot: string;
  whyGraphDbPath: string;
  codeGraphDbPath: string | null;
  anthropicApiKey: string | undefined;
  model: string;
  evidenceTtlMs: number;
}

const DEFAULT_TTL_DAYS = 14;

export function loadConfig(): WhyGraphConfig {
  const repoRoot = process.cwd();
  const ttlDaysRaw = Number.parseInt(
    process.env.WHYGRAPH_EVIDENCE_TTL_DAYS ?? '',
    10
  );
  const ttlDays = Number.isFinite(ttlDaysRaw) && ttlDaysRaw > 0 ? ttlDaysRaw : DEFAULT_TTL_DAYS;
  return {
    repoRoot,
    whyGraphDbPath:
      process.env.WHYGRAPH_DB ?? resolve(repoRoot, '.whygraph', 'whygraph.db'),
    codeGraphDbPath: process.env.CODEGRAPH_DB ?? null,
    anthropicApiKey: process.env.ANTHROPIC_API_KEY,
    model: process.env.WHYGRAPH_MODEL ?? 'claude-sonnet-4-6',
    evidenceTtlMs: ttlDays * 24 * 60 * 60 * 1000,
  };
}
