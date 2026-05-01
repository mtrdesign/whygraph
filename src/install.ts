import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { findCodeGraphDb } from './codegraph/reader.js';
import { openWhyGraphDb } from './db/client.js';
import type { RationaleBackend } from './config.js';

export interface InstallOpts {
  targetDir: string;
  backend: RationaleBackend;
  force: boolean;
}

const WHYGRAPH_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

export function cmdInstall(opts: InstallOpts): void {
  const target = resolve(opts.targetDir);
  if (!existsSync(target) || !statSync(target).isDirectory()) {
    console.error(`Target directory does not exist: ${target}`);
    process.exit(1);
  }

  const indexEntry = join(WHYGRAPH_ROOT, 'src', 'index.ts');
  if (!existsSync(indexEntry)) {
    console.error(`whygraph entry point not found: ${indexEntry}`);
    process.exit(1);
  }

  console.log(`Installing whygraph into ${target}`);
  console.log(`  whygraph source: ${WHYGRAPH_ROOT}`);
  console.log(`  backend:         ${opts.backend}`);

  const cgDb = findCodeGraphDb(target);
  if (!cgDb) {
    console.error('');
    console.error(
      'No CodeGraph DB found at <target>/.codegraph/codegraph.db (or any parent).'
    );
    console.error('Run CodeGraph in this project first, then re-run install.');
    process.exit(1);
  }
  console.log(`  CodeGraph DB:    ${cgDb}`);

  const wgDir = join(target, '.whygraph');
  const wgDb = join(wgDir, 'whygraph.db');
  if (!existsSync(wgDir)) mkdirSync(wgDir, { recursive: true });
  if (existsSync(wgDb)) {
    console.log(`  WhyGraph DB:     ${wgDb} (exists, kept)`);
  } else {
    const db = openWhyGraphDb(wgDb);
    db.close();
    console.log(`  WhyGraph DB:     ${wgDb} (created)`);
  }
  const wgGitignore = join(wgDir, '.gitignore');
  if (!existsSync(wgGitignore)) {
    writeFileSync(
      wgGitignore,
      '# WhyGraph local data — do not commit\n*.db\n*.db-wal\n*.db-shm\n'
    );
    console.log(`  .gitignore:      ${wgGitignore} (created)`);
  }

  const skillSrc = join(WHYGRAPH_ROOT, 'examples', 'skills', 'whygraph-pre-edit');
  const skillDst = join(target, '.claude', 'skills', 'whygraph-pre-edit');
  copyDirRecursive(skillSrc, skillDst, opts.force);
  console.log(`  Skill:           ${skillDst}`);

  const cmdSrc = join(WHYGRAPH_ROOT, 'examples', 'commands', 'rationale.md');
  const cmdDstDir = join(target, '.claude', 'commands');
  const cmdDst = join(cmdDstDir, 'rationale.md');
  if (!existsSync(cmdDstDir)) mkdirSync(cmdDstDir, { recursive: true });
  if (existsSync(cmdDst) && !opts.force) {
    console.log(
      `  /rationale:      ${cmdDst} (exists, skipped — pass --force to overwrite)`
    );
  } else {
    copyFileSync(cmdSrc, cmdDst);
    console.log(`  /rationale:      ${cmdDst}`);
  }

  const mcpPath = join(target, '.mcp.json');
  const env: Record<string, string> = {
    CODEGRAPH_DB: cgDb,
    WHYGRAPH_DB: wgDb,
    WHYGRAPH_RATIONALE_BACKEND: opts.backend,
  };
  if (opts.backend === 'api') {
    env.ANTHROPIC_API_KEY = '${ANTHROPIC_API_KEY}';
  }
  const whygraphServer = {
    command: 'npx',
    args: ['tsx', indexEntry, 'mcp'],
    env,
  };

  type McpFile = { mcpServers?: Record<string, unknown> } & Record<string, unknown>;
  let mcpJson: McpFile = {};
  if (existsSync(mcpPath)) {
    try {
      mcpJson = JSON.parse(readFileSync(mcpPath, 'utf8')) as McpFile;
    } catch (err) {
      console.error(`Failed to parse existing ${mcpPath}: ${(err as Error).message}`);
      process.exit(1);
    }
  }
  if (!mcpJson.mcpServers) mcpJson.mcpServers = {};
  if (mcpJson.mcpServers.whygraph && !opts.force) {
    console.log(
      `  .mcp.json:       ${mcpPath} already has a 'whygraph' entry — left untouched (pass --force to overwrite)`
    );
  } else {
    mcpJson.mcpServers.whygraph = whygraphServer;
    writeFileSync(mcpPath, `${JSON.stringify(mcpJson, null, 2)}\n`);
    console.log(`  .mcp.json:       ${mcpPath} (whygraph entry written)`);
  }

  console.log('');
  console.log('Done. Next steps:');
  console.log('  1. Restart Claude Code in this project so it picks up .mcp.json, the skill, and the slash command.');
  if (opts.backend === 'api') {
    console.log('  2. Export ANTHROPIC_API_KEY in the shell where you launch Claude Code.');
  } else {
    console.log('  2. Ensure the `claude` CLI is installed and signed into your Pro/Max subscription.');
  }
  console.log('  3. Try the /rationale slash command in Claude Code, or let the skill trigger before edits.');
}

function copyDirRecursive(src: string, dst: string, force: boolean): void {
  if (!existsSync(dst)) mkdirSync(dst, { recursive: true });
  for (const entry of readdirSync(src, { withFileTypes: true })) {
    const s = join(src, entry.name);
    const d = join(dst, entry.name);
    if (entry.isDirectory()) {
      copyDirRecursive(s, d, force);
    } else if (!existsSync(d) || force) {
      copyFileSync(s, d);
    }
  }
}
