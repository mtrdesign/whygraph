import type { CodeGraphNode } from '../codegraph/reader.js';
import type { GitEvidenceCollector } from './git.js';
import type { GitHubEvidenceCollector } from './github.js';
import type { EvidenceRow } from './store.js';

export interface BlamePayload {
  author: string;
  author_email: string;
  author_time: number;
  summary: string;
  line_count: number;
  line_total: number;
}

export interface CommitPayload {
  subject: string;
  body: string;
  author: string;
  author_email: string;
  author_time: number;
  committer: string;
  committer_email: string;
  committer_time: number;
  parents: string[];
}

export function collectGitEvidence(
  git: GitEvidenceCollector,
  node: CodeGraphNode
): EvidenceRow[] {
  const blame = git.blameLineRange(node.file_path, node.start_line, node.end_line);
  if (blame.length === 0) return [];

  const lineTotal = node.end_line - node.start_line + 1;
  const rows: EvidenceRow[] = [];

  for (const b of blame) {
    const payload: BlamePayload = {
      author: b.author,
      author_email: b.authorEmail,
      author_time: b.authorTime,
      summary: b.summary,
      line_count: b.lineCount,
      line_total: lineTotal,
    };
    rows.push({ source: 'git_blame', ref: b.commit, payload });
  }

  for (const b of blame) {
    const info = git.commitInfo(b.commit);
    if (!info) continue;
    const payload: CommitPayload = {
      subject: info.subject,
      body: info.body,
      author: info.author,
      author_email: info.authorEmail,
      author_time: info.authorTime,
      committer: info.committer,
      committer_email: info.committerEmail,
      committer_time: info.committerTime,
      parents: info.parents,
    };
    rows.push({ source: 'git_commit', ref: info.sha, payload });
  }

  return rows;
}

export function collectGitHubEvidence(
  github: GitHubEvidenceCollector,
  gitRows: EvidenceRow[]
): EvidenceRow[] {
  if (!github.isAvailable()) return [];

  const commitShas = new Set<string>();
  for (const row of gitRows) {
    if (row.source === 'git_commit' && row.ref) commitShas.add(row.ref);
  }

  const prNumbers = new Set<number>();
  for (const sha of commitShas) {
    for (const num of github.prNumbersForCommit(sha)) prNumbers.add(num);
  }

  const rows: EvidenceRow[] = [];
  const issueNumbers = new Set<number>();

  for (const num of prNumbers) {
    const pr = github.pr(num);
    if (!pr) continue;
    rows.push({ source: 'pr', ref: String(pr.number), payload: pr });
    for (const issueNum of pr.closes_issues) issueNumbers.add(issueNum);
  }

  for (const num of issueNumbers) {
    const issue = github.issue(num);
    if (!issue) continue;
    rows.push({ source: 'issue', ref: String(issue.number), payload: issue });
  }

  return rows;
}
