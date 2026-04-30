#!/usr/bin/env node
import { loadConfig, type WhyGraphConfig } from './config.js';
import { CodeGraphReader, findCodeGraphDb } from './codegraph/reader.js';
import { openWhyGraphDb } from './db/client.js';
import { GitEvidenceCollector } from './evidence/git.js';
import { collectGitEvidence } from './evidence/collector.js';
import { EvidenceStore } from './evidence/store.js';

function usage(): never {
  console.error('Usage: whygraph <command> [args]');
  console.error('Commands:');
  console.error('  init                      Create the WhyGraph DB at .whygraph/whygraph.db');
  console.error('  codegraph-stats           Print summary stats from the CodeGraph DB');
  console.error('  ingest                    Collect git evidence for every CodeGraph node');
  console.error('  evidence <node|qname>     Show stored evidence for a symbol');
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

function main(): void {
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
    default:
      usage();
  }
}

main();
