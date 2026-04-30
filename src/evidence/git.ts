import { execFileSync } from 'node:child_process';

export interface GitBlameEntry {
  commit: string;
  author: string;
  authorEmail: string;
  authorTime: number;     // unix seconds
  summary: string;        // first line of the commit message
  lineCount: number;      // # lines in the queried range attributed to this commit
}

export interface GitCommitInfo {
  sha: string;
  author: string;
  authorEmail: string;
  authorTime: number;
  committer: string;
  committerEmail: string;
  committerTime: number;
  parents: string[];
  subject: string;
  body: string;
}

export class GitEvidenceCollector {
  private readonly commitCache = new Map<string, GitCommitInfo>();

  constructor(private readonly repoRoot: string) {}

  blameLineRange(
    filePath: string,
    startLine: number,
    endLine: number
  ): GitBlameEntry[] {
    if (endLine < startLine) return [];
    const stdout = this.git([
      'blame',
      '--line-porcelain',
      '-L',
      `${startLine},${endLine}`,
      '--',
      filePath,
    ]);
    if (stdout === null) return [];
    return parseLinePorcelain(stdout);
  }

  commitInfo(sha: string): GitCommitInfo | null {
    const cached = this.commitCache.get(sha);
    if (cached) return cached;

    const meta = this.git([
      'log',
      '-1',
      '--format=%H%n%an%n%ae%n%at%n%cn%n%ce%n%ct%n%P%n%s',
      sha,
    ]);
    if (meta === null) return null;

    const body = this.git(['log', '-1', '--format=%B', sha]) ?? '';

    const parts = meta.replace(/\n$/, '').split('\n');
    if (parts.length < 9) return null;
    const info: GitCommitInfo = {
      sha: parts[0],
      author: parts[1],
      authorEmail: parts[2],
      authorTime: Number.parseInt(parts[3], 10) || 0,
      committer: parts[4],
      committerEmail: parts[5],
      committerTime: Number.parseInt(parts[6], 10) || 0,
      parents: parts[7] ? parts[7].split(' ').filter(Boolean) : [],
      subject: parts.slice(8).join('\n'),
      body: body.replace(/\n+$/, ''),
    };
    this.commitCache.set(sha, info);
    return info;
  }

  private git(args: string[]): string | null {
    try {
      return execFileSync('git', args, {
        cwd: this.repoRoot,
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'pipe'],
        maxBuffer: 32 * 1024 * 1024,
      });
    } catch {
      return null;
    }
  }
}

const SHA_HEADER = /^[0-9a-f]{7,64} \d+ \d+(?: \d+)?$/;

function parseLinePorcelain(stdout: string): GitBlameEntry[] {
  const entries = new Map<string, GitBlameEntry>();
  const lines = stdout.split('\n');
  let i = 0;

  while (i < lines.length) {
    const header = lines[i++];
    if (!header || !SHA_HEADER.test(header)) continue;
    const sha = header.slice(0, header.indexOf(' '));

    let author = '';
    let authorEmail = '';
    let authorTime = 0;
    let summary = '';

    while (i < lines.length && !lines[i].startsWith('\t')) {
      const line = lines[i++];
      if (line.startsWith('author ')) author = line.slice(7);
      else if (line.startsWith('author-mail ')) {
        authorEmail = line.slice(12).replace(/^<|>$/g, '');
      } else if (line.startsWith('author-time ')) {
        authorTime = Number.parseInt(line.slice(12), 10) || 0;
      } else if (line.startsWith('summary ')) {
        summary = line.slice(8);
      }
    }
    if (i < lines.length && lines[i].startsWith('\t')) i++;

    const existing = entries.get(sha);
    if (existing) {
      existing.lineCount++;
    } else {
      entries.set(sha, { commit: sha, author, authorEmail, authorTime, summary, lineCount: 1 });
    }
  }

  return [...entries.values()].sort((a, b) => b.lineCount - a.lineCount);
}
