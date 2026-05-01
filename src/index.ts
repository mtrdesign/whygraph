#!/usr/bin/env node
import { loadConfig, type WhyGraphConfig } from './config.js';
import { CodeGraphReader, findCodeGraphDb } from './codegraph/reader.js';
import { openWhyGraphDb } from './db/client.js';
import { GitEvidenceCollector } from './evidence/git.js';
import { collectGitEvidence } from './evidence/collector.js';
import { EvidenceStore, computeBundleHash } from './evidence/store.js';
import { computeConfidence } from './rationale/confidence.js';
import { RationaleGenerator } from './rationale/generator.js';
import { RationaleStore } from './rationale/store.js';
import { PROMPT_VERSION } from './rationale/prompt.js';

function usage(): never {
  console.error('Usage: whygraph <command> [args]');
  console.error('Commands:');
  console.error('  init                      Create the WhyGraph DB at .whygraph/whygraph.db');
  console.error('  codegraph-stats           Print summary stats from the CodeGraph DB');
  console.error('  ingest                    Collect git evidence for every CodeGraph node');
  console.error('  evidence <node|qname>     Show stored evidence for a symbol');
  console.error('  rationale <node|qname>    Show or generate rationale for a symbol (--force to regenerate)');
  process.exit(1);
}

function resolveCodeGraphDb(config: WhyGraphConfig): string {
  const path = config.codeGraphDbPath ?? findCodeGraphDb(config.repoRoot);
  if (!path) {
    console.error('No CodeGraph DB found.');
    console.error('Run CodeGraph in this project, or set CODEGRAPH_DB to an absolute path.');
    process.exit(1);
  }
  return path;
}

function cmdInit(config: WhyGraphConfig): void {
  const db = openWhyGraphDb(config.whyGraphDbPath);
  db.close();
  console.log(`Initialized WhyGraph DB at ${config.whyGraphDbPath}`);
}

function cmdCodeGraphStats(config: WhyGraphConfig): void {
  const dbPath = resolveCodeGraphDb(config);
  const reader = new CodeGraphReader(dbPath);
  try {
    console.log(`CodeGraph DB: ${dbPath}`);
    console.log(`  files: ${reader.countFiles()}`);
    console.log(`  nodes: ${reader.countNodes()}`);
    console.log(`  edges: ${reader.countEdges()}`);

    const langs = reader.languageBreakdown();
    if (langs.length > 0) {
      console.log('  languages:');
      for (const { language, nodes } of langs) {
        console.log(`    ${language.padEnd(16)} ${nodes}`);
      }
    }

    const kinds = reader.kindBreakdown().slice(0, 10);
    if (kinds.length > 0) {
      console.log('  top kinds:');
      for (const { kind, nodes } of kinds) {
        console.log(`    ${kind.padEnd(16)} ${nodes}`);
      }
    }
  } finally {
    reader.close();
  }
}

function cmdIngest(config: WhyGraphConfig): void {
  const codeGraphPath = resolveCodeGraphDb(config);
  const reader = new CodeGraphReader(codeGraphPath);
  const db = openWhyGraphDb(config.whyGraphDbPath);
  const store = new EvidenceStore(db);
  const git = new GitEvidenceCollector(config.repoRoot);

  const startedAt = Date.now();
  const startStmt = db.prepare('INSERT INTO ingest_runs (started_at) VALUES (?)');
  const finishStmt = db.prepare(
    'UPDATE ingest_runs SET finished_at = ?, symbols_seen = ?, symbols_with_evidence = ? WHERE id = ?'
  );
  const runId = Number(startStmt.run(startedAt).lastInsertRowid);

  let seen = 0;
  let withEvidence = 0;
  try {
    for (const node of reader.iterateNodes()) {
      seen++;
      const rows = collectGitEvidence(git, node);
      if (rows.length === 0) continue;
      store.replace(node.id, node.qualified_name, rows);
      withEvidence++;
      if (seen % 100 === 0) {
        console.error(`  ${seen} symbols, ${withEvidence} with evidence...`);
      }
    }
    finishStmt.run(Date.now(), seen, withEvidence, runId);
    console.log(
      `Ingest complete: ${seen} symbols seen, ${withEvidence} with git evidence`
    );
    console.log(`Elapsed: ${((Date.now() - startedAt) / 1000).toFixed(1)}s`);
  } finally {
    reader.close();
    db.close();
  }
}

function cmdEvidence(config: WhyGraphConfig, target: string | undefined): void {
  if (!target) {
    console.error('Usage: whygraph evidence <node-id|qualified-name>');
    process.exit(1);
  }
  const codeGraphPath = resolveCodeGraphDb(config);
  const reader = new CodeGraphReader(codeGraphPath);
  const db = openWhyGraphDb(config.whyGraphDbPath);
  const store = new EvidenceStore(db);

  try {
    const node =
      reader.getNode(target) ?? reader.findNodesByQualifiedName(target)[0];
    if (!node) {
      console.error(`Symbol not found in CodeGraph: ${target}`);
      process.exit(1);
    }

    const rows = store.forNode(node.id);
    console.log(
      `${node.qualified_name}  (${node.kind}, ${node.file_path}:${node.start_line}-${node.end_line})`
    );
    console.log(`evidence rows: ${rows.length}`);
    for (const r of rows) {
      const payload = r.payload as Record<string, unknown> | null;
      const summary =
        payload && typeof payload === 'object'
          ? (payload.summary as string | undefined) ??
            (payload.subject as string | undefined) ??
            ''
          : '';
      console.log(`  [${r.source}] ${r.ref ?? '-'}  ${summary}`);
    }
  } finally {
    reader.close();
    db.close();
  }
}

async function cmdRationale(
  config: WhyGraphConfig,
  target: string | undefined,
  force: boolean
): Promise<void> {
  if (!target) {
    console.error('Usage: whygraph rationale <node-id|qualified-name> [--force]');
    process.exit(1);
  }

  const codeGraphPath = resolveCodeGraphDb(config);
  const reader = new CodeGraphReader(codeGraphPath);
  const db = openWhyGraphDb(config.whyGraphDbPath);
  const evidenceStore = new EvidenceStore(db);
  const rationaleStore = new RationaleStore(db);

  try {
    const node =
      reader.getNode(target) ?? reader.findNodesByQualifiedName(target)[0];
    if (!node) {
      console.error(`Symbol not found in CodeGraph: ${target}`);
      process.exit(1);
    }

    const evidence = evidenceStore.forNode(node.id);
    if (evidence.length === 0) {
      console.error(
        `No evidence for ${node.qualified_name}. Run \`whygraph ingest\` first.`
      );
      process.exit(1);
    }

    const bundleHash =
      evidenceStore.bundleHashFor(node.id) ??
      computeBundleHash(
        evidence.map((e) => ({ source: e.source, ref: e.ref, payload: e.payload }))
      );

    const cached = rationaleStore.get(node.id);
    const cacheHit =
      !force &&
      cached &&
      cached.bundle_hash === bundleHash &&
      cached.prompt_version === PROMPT_VERSION &&
      cached.model === config.model;

    let record = cached;
    if (!cacheHit) {
      if (!config.anthropicApiKey) {
        console.error(
          'ANTHROPIC_API_KEY is not set. Export it to generate a rationale.'
        );
        process.exit(1);
      }

      const generator = new RationaleGenerator({
        apiKey: config.anthropicApiKey,
        model: config.model,
      });

      console.error(
        `Generating rationale for ${node.qualified_name} (model=${config.model}, prompt=${PROMPT_VERSION})...`
      );
      const result = await generator.generate({ node }, evidence);
      const confidence = computeConfidence(evidence);
      record = rationaleStore.upsert({
        node_id: node.id,
        bundle_hash: bundleHash,
        prompt_version: result.promptVersion,
        model: result.model,
        ...result.rationale,
        confidence,
      });
      console.error(
        `  tokens: in=${result.usage.inputTokens} cache_read=${result.usage.cacheReadInputTokens} cache_create=${result.usage.cacheCreationInputTokens} out=${result.usage.outputTokens}`
      );
    }

    if (!record) {
      console.error('Unexpected: no rationale record after generate path.');
      process.exit(1);
    }

    printRationale(node.qualified_name, record, cacheHit ? 'cached' : 'generated');
  } finally {
    reader.close();
    db.close();
  }
}

function printRationale(
  qname: string,
  r: {
    purpose: string;
    why: string;
    constraints: string[];
    tradeoffs: string[];
    risks: string[];
    confidence: number;
    model: string;
    prompt_version: string;
    bundle_hash: string;
    generated_at: number;
  },
  source: 'cached' | 'generated'
): void {
  console.log(`${qname}  [${source}]`);
  console.log(
    `  model=${r.model}  prompt=${r.prompt_version}  confidence=${r.confidence.toFixed(2)}  bundle=${r.bundle_hash.slice(0, 12)}`
  );
  console.log('');
  console.log(`Purpose: ${r.purpose}`);
  console.log('');
  console.log('Why:');
  console.log(`  ${r.why}`);
  printList('Constraints', r.constraints);
  printList('Tradeoffs', r.tradeoffs);
  printList('Risks', r.risks);
}

function printList(label: string, items: string[]): void {
  console.log('');
  console.log(`${label}:`);
  if (items.length === 0) {
    console.log('  (none)');
    return;
  }
  for (const item of items) console.log(`  - ${item}`);
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  const [cmd, ...rest] = args;
  if (!cmd) usage();

  const config = loadConfig();

  switch (cmd) {
    case 'init':
      return cmdInit(config);
    case 'codegraph-stats':
      return cmdCodeGraphStats(config);
    case 'ingest':
      return cmdIngest(config);
    case 'evidence':
      return cmdEvidence(config, rest[0]);
    case 'rationale': {
      const positional = rest.filter((a) => !a.startsWith('--'));
      const force = rest.includes('--force');
      return cmdRationale(config, positional[0], force);
    }
    default:
      usage();
  }
}

main().catch((err) => {
  console.error(err instanceof Error ? err.stack ?? err.message : err);
  process.exit(1);
});
