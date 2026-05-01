import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import type Database from 'better-sqlite3';
import { z } from 'zod';
import { loadConfig } from '../config.js';
import {
  CodeGraphReader,
  findCodeGraphDb,
  type CodeGraphNode,
} from '../codegraph/reader.js';
import { openWhyGraphDb } from '../db/client.js';
import { GitEvidenceCollector } from '../evidence/git.js';
import { GitHubEvidenceCollector } from '../evidence/github.js';
import { EvidenceService, type CollectionResult } from '../evidence/service.js';
import { EvidenceStore } from '../evidence/store.js';
import { computeConfidence } from '../rationale/confidence.js';
import { RationaleGenerator } from '../rationale/generator.js';
import { RationaleStore, type RationaleRecord } from '../rationale/store.js';
import { PROMPT_VERSION } from '../rationale/prompt.js';

const ResponseFormat = z.enum(['markdown', 'json']);

const PreEditBriefInputShape = {
  target: z
    .string()
    .min(1)
    .describe(
      "Symbol identifier — either a CodeGraph node ID or a qualified_name (e.g. 'config.loadConfig')."
    ),
  force: z
    .boolean()
    .default(false)
    .describe(
      'If true, bypass the rationale cache and regenerate via Claude. Default: false.'
    ),
  refresh_evidence: z
    .boolean()
    .default(false)
    .describe(
      'If true, recollect git/GitHub evidence even if cached and fresh. Implies bypassing the rationale cache. Default: false.'
    ),
  response_format: ResponseFormat.default('markdown').describe(
    "Output format: 'markdown' for a human-readable brief or 'json' for full structured data."
  ),
};

const EvidenceForInputShape = {
  target: z
    .string()
    .min(1)
    .describe(
      'Symbol identifier — either a CodeGraph node ID or a qualified_name.'
    ),
  refresh: z
    .boolean()
    .default(false)
    .describe('If true, recollect evidence even if cached and fresh. Default: false.'),
  response_format: ResponseFormat.default('markdown').describe(
    "Output format: 'markdown' or 'json'."
  ),
};

interface ServerDeps {
  reader: CodeGraphReader;
  db: Database.Database;
  service: EvidenceService;
  rationaleStore: RationaleStore;
  ensureGenerator(): RationaleGenerator;
  model: string;
}

export async function runMcpServer(): Promise<void> {
  const config = loadConfig();
  const codeGraphPath =
    config.codeGraphDbPath ?? findCodeGraphDb(config.repoRoot);
  if (!codeGraphPath) {
    console.error(
      '[whygraph-mcp] No CodeGraph DB found. Set CODEGRAPH_DB or run from a CodeGraph-initialized project.'
    );
    process.exit(1);
  }

  const reader = new CodeGraphReader(codeGraphPath);
  const db = openWhyGraphDb(config.whyGraphDbPath);
  const evidenceStore = new EvidenceStore(db);
  const rationaleStore = new RationaleStore(db);
  const git = new GitEvidenceCollector(config.repoRoot);
  const github = new GitHubEvidenceCollector(config.repoRoot);
  const service = new EvidenceService(
    evidenceStore,
    git,
    github,
    config.repoRoot,
    { ttlMs: config.evidenceTtlMs }
  );

  let generator: RationaleGenerator | null = null;
  function ensureGenerator(): RationaleGenerator {
    if (generator) return generator;
    if (config.rationaleBackend === 'api' && !config.anthropicApiKey) {
      throw new Error(
        'ANTHROPIC_API_KEY is not set. Set it in the MCP server environment, or set WHYGRAPH_RATIONALE_BACKEND=claude_cli to use the local claude CLI.'
      );
    }
    generator = new RationaleGenerator({
      backend: config.rationaleBackend,
      apiKey: config.anthropicApiKey,
      model: config.model,
    });
    return generator;
  }

  const deps: ServerDeps = {
    reader,
    db,
    service,
    rationaleStore,
    ensureGenerator,
    model: config.model,
  };

  const server = new McpServer({
    name: 'whygraph-mcp-server',
    version: '0.0.1',
  });

  server.registerTool(
    'whygraph_rationale_pre_edit_brief',
    {
      title: 'WhyGraph: Rationale Pre-Edit Brief',
      description:
        'Return the rationale for a code symbol before editing it: purpose, why it exists, constraints to preserve, tradeoffs, and risks of modification. ' +
        'Lazily collects evidence (git blame + commits, plus GitHub PRs/issues if available) on first request for a symbol; subsequent requests reuse the cache for ~14 days unless the file has new commits. ' +
        'Returns the cached rationale when (bundle_hash, prompt_version, model) matches; otherwise calls Claude. ' +
        'Use this BEFORE making non-trivial changes to a symbol so the edit respects the original intent.\n\n' +
        'Args:\n' +
        '  - target (string): Either a CodeGraph node ID or a qualified_name (e.g. "config.loadConfig").\n' +
        '  - force (boolean, optional): Bypass the rationale cache and regenerate. Default false.\n' +
        '  - refresh_evidence (boolean, optional): Recollect upstream evidence even if cached. Default false.\n' +
        '  - response_format ("markdown" | "json", optional): Default "markdown".\n\n' +
        'Returns: structured rationale with confidence (0-1, capped at 0.85 in v0). ' +
        'Returns isError=true with a clear message when the symbol is unknown, the file has no git history, or the API key is missing on a cache miss.',
      inputSchema: PreEditBriefInputShape,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    async (input) => {
      try {
        return await handlePreEditBrief(deps, input);
      } catch (err) {
        return errorResult(formatError(err));
      }
    }
  );

  server.registerTool(
    'whygraph_evidence_for',
    {
      title: 'WhyGraph: Evidence For Symbol',
      description:
        'Return the raw evidence rows stored for a symbol — git commits, blame, and (when available) PRs/issues. ' +
        'Auto-collects on first request; subsequent requests reuse the cache for ~14 days unless the file has new commits. ' +
        'Useful for inspecting what a generated rationale was based on, or for debugging. Never calls Claude.\n\n' +
        'Args:\n' +
        '  - target (string): CodeGraph node ID or qualified_name.\n' +
        '  - refresh (boolean, optional): Recollect evidence even if cached. Default false.\n' +
        '  - response_format ("markdown" | "json", optional): Default "markdown".\n\n' +
        'Returns: list of evidence items each with source, ref (commit sha / PR# / etc.), payload, and collected_at timestamp; plus the cache source ("cache" or "collected").',
      inputSchema: EvidenceForInputShape,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    async (input) => {
      try {
        return handleEvidenceFor(deps, input);
      } catch (err) {
        return errorResult(formatError(err));
      }
    }
  );

  const cleanup = (): void => {
    try {
      reader.close();
    } catch {
      /* ignore */
    }
    try {
      db.close();
    } catch {
      /* ignore */
    }
  };
  process.on('SIGINT', () => {
    cleanup();
    process.exit(0);
  });
  process.on('SIGTERM', () => {
    cleanup();
    process.exit(0);
  });

  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(
    `[whygraph-mcp] connected via stdio (codegraph=${codeGraphPath}, whygraph=${config.whyGraphDbPath}, model=${config.model})`
  );
}

async function handlePreEditBrief(
  deps: ServerDeps,
  input: {
    target: string;
    force: boolean;
    refresh_evidence: boolean;
    response_format: 'markdown' | 'json';
  }
) {
  const node = resolveSymbol(deps.reader, input.target);
  if (!node) {
    return errorResult(`Symbol not found in CodeGraph: ${input.target}`);
  }

  console.error(`[whygraph-mcp] rationale: ${node.qualified_name}`);
  const collection = deps.service.forNode(node, { force: input.refresh_evidence });
  if (collection.evidence.length === 0) {
    return errorResult(
      `No evidence for ${node.qualified_name}: file has no git history (${node.file_path}).`
    );
  }

  const cached = deps.rationaleStore.get(node.id);
  const cacheHit =
    !input.force &&
    !!cached &&
    cached.bundle_hash === collection.bundleHash &&
    cached.prompt_version === PROMPT_VERSION &&
    cached.model === deps.model;

  let record: RationaleRecord | null = cached;
  let source: 'cached' | 'generated' = 'cached';
  if (!cacheHit) {
    const result = await deps.ensureGenerator().generate({ node }, collection.evidence);
    const confidence = computeConfidence(collection.evidence);
    record = deps.rationaleStore.upsert({
      node_id: node.id,
      bundle_hash: collection.bundleHash,
      prompt_version: result.promptVersion,
      model: result.model,
      ...result.rationale,
      confidence,
    });
    source = 'generated';
  }

  if (!record) {
    return errorResult('Internal: no rationale record after generate path.');
  }

  const output = {
    qualified_name: node.qualified_name,
    kind: node.kind,
    location: `${node.file_path}:${node.start_line}-${node.end_line}`,
    source,
    evidence_source: collection.source,
    model: record.model,
    prompt_version: record.prompt_version,
    bundle_hash: record.bundle_hash,
    confidence: record.confidence,
    generated_at: record.generated_at,
    purpose: record.purpose,
    why: record.why,
    constraints: record.constraints,
    tradeoffs: record.tradeoffs,
    risks: record.risks,
  };

  const text =
    input.response_format === 'json'
      ? JSON.stringify(output, null, 2)
      : formatRationaleMarkdown(output);

  return {
    content: [{ type: 'text' as const, text }],
    structuredContent: output,
  };
}

function handleEvidenceFor(
  deps: ServerDeps,
  input: {
    target: string;
    refresh: boolean;
    response_format: 'markdown' | 'json';
  }
) {
  const node = resolveSymbol(deps.reader, input.target);
  if (!node) {
    return errorResult(`Symbol not found in CodeGraph: ${input.target}`);
  }
  console.error(`[whygraph-mcp] evidence: ${node.qualified_name}`);
  const collection = deps.service.forNode(node, { force: input.refresh });
  const output = {
    qualified_name: node.qualified_name,
    node_id: node.id,
    location: `${node.file_path}:${node.start_line}-${node.end_line}`,
    source: collection.source,
    bundle_hash: collection.bundleHash,
    head_at_collection: collection.headAtCollection,
    collected_at: collection.collectedAt,
    evidence: collection.evidence.map((e) => ({
      source: e.source,
      ref: e.ref,
      collected_at: e.collected_at,
      payload: e.payload,
    })),
  };
  const text =
    input.response_format === 'json'
      ? JSON.stringify(output, null, 2)
      : formatEvidenceMarkdown(output, collection);
  return {
    content: [{ type: 'text' as const, text }],
    structuredContent: output,
  };
}

function resolveSymbol(
  reader: CodeGraphReader,
  target: string
): CodeGraphNode | null {
  return (
    reader.getNode(target) ?? reader.findNodesByQualifiedName(target)[0] ?? null
  );
}

function errorResult(message: string) {
  return {
    isError: true,
    content: [{ type: 'text' as const, text: message }],
  };
}

function formatError(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}

function formatRationaleMarkdown(o: {
  qualified_name: string;
  kind: string;
  location: string;
  source: 'cached' | 'generated';
  evidence_source: 'cache' | 'collected';
  model: string;
  prompt_version: string;
  bundle_hash: string;
  confidence: number;
  purpose: string;
  why: string;
  constraints: string[];
  tradeoffs: string[];
  risks: string[];
}): string {
  const lines = [
    `# Rationale: \`${o.qualified_name}\``,
    '',
    `- **Kind**: ${o.kind}`,
    `- **Location**: ${o.location}`,
    `- **Model**: ${o.model} (prompt ${o.prompt_version})`,
    `- **Confidence**: ${o.confidence.toFixed(2)}`,
    `- **Rationale**: ${o.source} · **Evidence**: ${o.evidence_source} (bundle ${o.bundle_hash.slice(0, 12)})`,
    '',
    `## Purpose`,
    o.purpose || '_(none)_',
    '',
    `## Why`,
    o.why || '_(none)_',
    '',
    `## Constraints`,
    o.constraints.length === 0
      ? '_(none)_'
      : o.constraints.map((c) => `- ${c}`).join('\n'),
    '',
    `## Tradeoffs`,
    o.tradeoffs.length === 0
      ? '_(none)_'
      : o.tradeoffs.map((t) => `- ${t}`).join('\n'),
    '',
    `## Risks`,
    o.risks.length === 0 ? '_(none)_' : o.risks.map((r) => `- ${r}`).join('\n'),
  ];
  return lines.join('\n');
}

function formatEvidenceMarkdown(
  o: {
    qualified_name: string;
    location: string;
    bundle_hash: string;
    head_at_collection: string | null;
  },
  collection: CollectionResult
): string {
  const lines = [
    `# Evidence: \`${o.qualified_name}\``,
    '',
    `- **Location**: ${o.location}`,
    `- **Source**: ${collection.source} (bundle ${o.bundle_hash.slice(0, 12)})`,
    `- **HEAD at collection**: ${o.head_at_collection?.slice(0, 12) ?? '(none)'}`,
    `- **Items**: ${collection.evidence.length}`,
    '',
  ];
  for (const e of collection.evidence) {
    const p = (e.payload ?? {}) as Record<string, unknown>;
    const summary =
      (p.summary as string | undefined) ??
      (p.subject as string | undefined) ??
      (p.title as string | undefined) ??
      '';
    const refStr = e.ref ? `\`${e.ref.slice(0, 12)}\`` : '`-`';
    lines.push(`- **${e.source}** ${refStr} — ${summary}`);
  }
  return lines.join('\n');
}
