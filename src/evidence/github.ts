import { execFileSync } from 'node:child_process';

export interface GitHubPRPayload {
  number: number;
  title: string;
  body: string;
  state: string;
  merged: boolean;
  merged_at: string | null;
  created_at: string | null;
  author: string;
  url: string;
  closes_issues: number[];
}

export interface GitHubIssuePayload {
  number: number;
  title: string;
  body: string;
  state: string;
  created_at: string | null;
  closed_at: string | null;
  author: string;
  url: string;
  labels: string[];
}

export class GitHubEvidenceCollector {
  private readonly prsByCommit = new Map<string, number[]>();
  private readonly prDetails = new Map<number, GitHubPRPayload | null>();
  private readonly issueDetails = new Map<number, GitHubIssuePayload | null>();
  readonly repo: string | null;
  private readonly available: boolean;

  constructor(repoRoot: string) {
    this.repo = detectGitHubRepo(repoRoot);
    this.available = this.repo !== null && hasGh();
  }

  isAvailable(): boolean {
    return this.available;
  }

  prNumbersForCommit(sha: string): number[] {
    if (!this.available || !this.repo) return [];
    const cached = this.prsByCommit.get(sha);
    if (cached) return cached;
    const out = this.gh([
      'api',
      `/repos/${this.repo}/commits/${sha}/pulls`,
    ]);
    let numbers: number[] = [];
    if (out !== null) {
      try {
        const parsed = JSON.parse(out) as Array<{ number: number; merged_at?: string | null }>;
        // Prefer merged PRs; fall back to all if there are no merged ones.
        const merged = parsed.filter((p) => p.merged_at).map((p) => p.number);
        numbers = merged.length > 0 ? merged : parsed.map((p) => p.number);
      } catch {
        numbers = [];
      }
    }
    this.prsByCommit.set(sha, numbers);
    return numbers;
  }

  pr(number: number): GitHubPRPayload | null {
    if (!this.available || !this.repo) return null;
    if (this.prDetails.has(number)) return this.prDetails.get(number) ?? null;
    const out = this.gh([
      'pr',
      'view',
      String(number),
      '--repo',
      this.repo,
      '--json',
      'number,title,body,state,merged,mergedAt,createdAt,author,url,closingIssuesReferences',
    ]);
    if (out === null) {
      this.prDetails.set(number, null);
      return null;
    }
    let raw: PrJson;
    try {
      raw = JSON.parse(out) as PrJson;
    } catch {
      this.prDetails.set(number, null);
      return null;
    }
    const payload: GitHubPRPayload = {
      number: raw.number,
      title: raw.title ?? '',
      body: raw.body ?? '',
      state: raw.state ?? '',
      merged: raw.merged ?? false,
      merged_at: raw.mergedAt ?? null,
      created_at: raw.createdAt ?? null,
      author: raw.author?.login ?? '',
      url: raw.url ?? '',
      closes_issues: (raw.closingIssuesReferences ?? []).map((r) => r.number),
    };
    this.prDetails.set(number, payload);
    return payload;
  }

  issue(number: number): GitHubIssuePayload | null {
    if (!this.available || !this.repo) return null;
    if (this.issueDetails.has(number)) return this.issueDetails.get(number) ?? null;
    const out = this.gh([
      'issue',
      'view',
      String(number),
      '--repo',
      this.repo,
      '--json',
      'number,title,body,state,createdAt,closedAt,author,url,labels',
    ]);
    if (out === null) {
      this.issueDetails.set(number, null);
      return null;
    }
    let raw: IssueJson;
    try {
      raw = JSON.parse(out) as IssueJson;
    } catch {
      this.issueDetails.set(number, null);
      return null;
    }
    const payload: GitHubIssuePayload = {
      number: raw.number,
      title: raw.title ?? '',
      body: raw.body ?? '',
      state: raw.state ?? '',
      created_at: raw.createdAt ?? null,
      closed_at: raw.closedAt ?? null,
      author: raw.author?.login ?? '',
      url: raw.url ?? '',
      labels: (raw.labels ?? []).map((l) => l.name),
    };
    this.issueDetails.set(number, payload);
    return payload;
  }

  private gh(args: string[]): string | null {
    try {
      return execFileSync('gh', args, {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'pipe'],
        maxBuffer: 32 * 1024 * 1024,
      });
    } catch {
      return null;
    }
  }
}

interface PrJson {
  number: number;
  title?: string;
  body?: string;
  state?: string;
  merged?: boolean;
  mergedAt?: string | null;
  createdAt?: string | null;
  author?: { login?: string };
  url?: string;
  closingIssuesReferences?: Array<{ number: number }>;
}

interface IssueJson {
  number: number;
  title?: string;
  body?: string;
  state?: string;
  createdAt?: string | null;
  closedAt?: string | null;
  author?: { login?: string };
  url?: string;
  labels?: Array<{ name: string }>;
}

function detectGitHubRepo(repoRoot: string): string | null {
  let url: string;
  try {
    url = execFileSync('git', ['-C', repoRoot, 'config', '--get', 'remote.origin.url'], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    }).trim();
  } catch {
    return null;
  }
  return parseGitHubRepo(url);
}

export function parseGitHubRepo(url: string): string | null {
  const ssh = url.match(/^git@github\.com:([^/]+\/[^/]+?)(?:\.git)?$/);
  if (ssh) return ssh[1];
  const https = url.match(/^https?:\/\/github\.com\/([^/]+\/[^/]+?)(?:\.git)?$/);
  if (https) return https[1];
  return null;
}

function hasGh(): boolean {
  try {
    execFileSync('gh', ['--version'], { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}
