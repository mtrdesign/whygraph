#!/usr/bin/env node
import { loadConfig, type WhyGraphConfig } from './config.js';
import { CodeGraphReader, findCodeGraphDb } from './codegraph/reader.js';
import { openWhyGraphDb } from './db/client.js';

function usage(): never {
  console.error('Usage: whygraph <command>');
  console.error('Commands:');
  console.error('  init              Create the WhyGraph DB at .whygraph/whygraph.db');
  console.error('  codegraph-stats   Print summary stats from the CodeGraph DB');
  process.exit(1);
}

function resolveCodeGraphDb(config: WhyGraphConfig): string {
  const path = config.codeGraphDbPath ?? findCodeGraphDb(config.repoRoot);
  if (!path) {
    console.error('No CodeGraph DB found.');
    console.error(
      'Run CodeGraph in this project, or set CODEGRAPH_DB to an absolute path.'
    );
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

function main(): void {
  const [cmd] = process.argv.slice(2);
  if (!cmd) usage();

  const config = loadConfig();

  switch (cmd) {
    case 'init':
      return cmdInit(config);
    case 'codegraph-stats':
      return cmdCodeGraphStats(config);
    default:
      usage();
  }
}

main();
