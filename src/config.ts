import { resolve } from 'node:path';

export interface WhyGraphConfig {
  repoRoot: string;
  whyGraphDbPath: string;
  codeGraphDbPath: string;
  anthropicApiKey: string | undefined;
  model: string;
}

export function loadConfig(): WhyGraphConfig {
  const repoRoot = process.cwd();
  return {
    repoRoot,
    whyGraphDbPath:
      process.env.WHYGRAPH_DB ?? resolve(repoRoot, '.whygraph', 'whygraph.db'),
    codeGraphDbPath:
      process.env.CODEGRAPH_DB ?? resolve(repoRoot, '.codegraph', 'codegraph.db'),
    anthropicApiKey: process.env.ANTHROPIC_API_KEY,
    model: process.env.WHYGRAPH_MODEL ?? 'claude-sonnet-4-6',
  };
}
