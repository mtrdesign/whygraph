#!/usr/bin/env node
import { loadConfig } from './config.js';
import { openWhyGraphDb } from './db/client.js';

function usage(): never {
  console.error('Usage: whygraph <command>');
  console.error('Commands:');
  console.error('  init    Create the WhyGraph DB at .whygraph/whygraph.db');
  process.exit(1);
}

function main(): void {
  const [cmd] = process.argv.slice(2);
  if (!cmd) usage();

  const config = loadConfig();

  switch (cmd) {
    case 'init': {
      const db = openWhyGraphDb(config.whyGraphDbPath);
      db.close();
      console.log(`Initialized WhyGraph DB at ${config.whyGraphDbPath}`);
      return;
    }
    default:
      usage();
  }
}

main();
