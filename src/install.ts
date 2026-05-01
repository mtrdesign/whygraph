import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { homedir } from 'node:os';
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
  global: boolean;
}

const WHYGRAPH_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

// Build an MCP launch tuple that pins the Node major to whichever Node ran
// `whygraph install`. Claude Code's harness inherits its own PATH (often a
// different Node major), and `npx`/`tsx` both have `#!/usr/bin/env node`
// shebangs — so even an absolute npx path re-resolves `node` from PATH and
// we still load native deps (better-sqlite3) under the wrong Node, hitting
// NODE_MODULE_VERSION crashes. Calling `node <abs tsx cli.mjs> ...` directly
// bypasses both shebangs and locks the subprocess to process.execPath.
function buildMcpLaunch(indexEntry: string): { command: string; args: string[] } {
  const nodeBin = process.execPath;
  const tsxCli = resolve(WHYGRAPH_ROOT, 'node_modules', 'tsx', 'dist', 'cli.mjs');
  if (!existsSync(tsxCli)) {
    console.error(
      `Could not find tsx CLI at ${tsxCli}. Run \`npm install\` in ${WHYGRAPH_ROOT} before installing whygraph.`
    );
    process.exit(1);
  }
  return { command: nodeBin, args: [tsxCli, indexEntry, 'mcp'] };
}

export function runInstall(opts: InstallOpts): void {
  if (opts.global) {
    cmdInstallGlobal(opts);
  } else {
    cmdInstall(opts);
  }
}

function cmdInstallGlobal(opts: InstallOpts): void {
  const indexEntry = join(WHYGRAPH_ROOT, 'src', 'index.ts');
  if (!existsSync(indexEntry)) {
    console.error(`whygraph entry point not found: ${indexEntry}`);
    process.exit(1);
  }

  if (!hasClaudeCli()) {
    console.error(
      'The `claude` CLI was not found on PATH. Install Claude Code first: https://docs.claude.com/en/docs/claude-code'
    );
    process.exit(1);
  }

  console.log(`Installing whygraph globally (user scope)`);
  console.log(`  whygraph source: ${WHYGRAPH_ROOT}`);
  console.log(`  backend:         ${opts.backend}`);

  // Skill + slash command at user scope.
  const userClaudeDir = join(homedir(), '.claude');
  const skillSrc = join(WHYGRAPH_ROOT, 'examples', 'skills', 'whygraph-pre-edit');
  const skillDst = join(userClaudeDir, 'skills', 'whygraph-pre-edit');
  copyDirRecursive(skillSrc, skillDst, opts.force);
  console.log(`  Skill:           ${skillDst}`);

  const cmdSrc = join(WHYGRAPH_ROOT, 'examples', 'commands', 'rationale.md');
  const cmdDstDir = join(userClaudeDir, 'commands');
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

  // Register the MCP server at user scope. No project-specific paths in env;
  // the server discovers .codegraph/.whygraph by walking up from cwd at runtime.
  const env: Record<string, string> = {
    WHYGRAPH_RATIONALE_BACKEND: opts.backend,
  };
  if (opts.backend === 'api') {
    env.ANTHROPIC_API_KEY = '${ANTHROPIC_API_KEY}';
  }
  const launch = buildMcpLaunch(indexEntry);
  const serverConfig = {
    type: 'stdio' as const,
    command: launch.command,
    args: launch.args,
    env,
  };

  if (mcpServerExists('whygraph', 'user')) {
    if (!opts.force) {
      console.log(
        `  MCP (user):      'whygraph' already registered — left untouched (pass --force to re-register)`
      );
      printGlobalNextSteps(opts);
      return;
    }
    console.log(`  MCP (user):      removing existing 'whygraph' (--force)`);
    runClaude(['mcp', 'remove', '-s', 'user', 'whygraph']);
  }

  const addResult = spawnSync(
    'claude',
    [
      'mcp',
      'add-json',
      '-s',
      'user',
      'whygraph',
      JSON.stringify(serverConfig),
    ],
    { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }
  );
  if (addResult.status !== 0) {
    console.error(`Failed to register MCP server via claude CLI:`);
    if (addResult.stdout) console.error(addResult.stdout.trim());
    if (addResult.stderr) console.error(addResult.stderr.trim());
    process.exit(1);
  }
  console.log(`  MCP (user):      whygraph registered (claude mcp add-json -s user)`);

  printGlobalNextSteps(opts);
}

function printGlobalNextSteps(opts: InstallOpts): void {
  console.log('');
  console.log('Done. Next steps:');
  console.log('  1. Restart Claude Code so it picks up the new MCP server, skill, and slash command.');
  if (opts.backend === 'api') {
    console.log('  2. Export ANTHROPIC_API_KEY in the shell where you launch Claude Code.');
  } else {
    console.log('  2. Make sure you are signed into your Claude Pro/Max subscription (claude already running).');
  }
  console.log('  3. In any project that has a .codegraph/codegraph.db, try /rationale <SymbolName>.');
  console.log('     The .whygraph/whygraph.db is created automatically on first call.');
}

function hasClaudeCli(): boolean {
  try {
    execFileSync('claude', ['--version'], { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}

function mcpServerExists(name: string, scope: 'user' | 'project' | 'local'): boolean {
  const result = spawnSync('claude', ['mcp', 'get', name], {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (result.status !== 0) return false;
  const out = `${result.stdout ?? ''}`;
  // `claude mcp get` reports the scope it found — match it loosely.
  return out.toLowerCase().includes(scope);
}

function runClaude(args: string[]): void {
  const r = spawnSync('claude', args, { stdio: ['ignore', 'pipe', 'pipe'], encoding: 'utf8' });
  if (r.status !== 0) {
    console.error(`claude ${args.join(' ')} failed:`);
    if (r.stdout) console.error(r.stdout.trim());
    if (r.stderr) console.error(r.stderr.trim());
    process.exit(1);
  }
}

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
  const launch = buildMcpLaunch(indexEntry);
  const whygraphServer = {
    command: launch.command,
    args: launch.args,
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
