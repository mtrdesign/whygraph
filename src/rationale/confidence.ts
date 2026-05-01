import type { EvidenceRecord } from '../evidence/store.js';

// v0 caps confidence at 0.85 because we do not yet detect refactor lineage —
// any rationale could silently be inheriting from a renamed/moved symbol.
// Lift the cap once lineage detection lands.
const V0_CONFIDENCE_CEILING = 0.85;

const KNOWN_SOURCES = 5; // git_blame, git_commit, pr, issue, docstring/test_ref

export function computeConfidence(rows: EvidenceRecord[]): number {
  if (rows.length === 0) return 0;

  const sources = new Set(rows.map((r) => r.source));
  const commitShas = new Set(
    rows.filter((r) => r.source === 'git_commit' && r.ref).map((r) => r.ref!)
  );

  const newest = rows.reduce((acc, r) => Math.max(acc, payloadTime(r)), 0);
  const recency = recencyScore(newest);
  const commitDepth = commitDepthScore(commitShas.size);
  const diversity = sources.size / KNOWN_SOURCES;

  const raw = 0.4 * recency + 0.4 * commitDepth + 0.2 * diversity;
  return clamp(raw, 0, V0_CONFIDENCE_CEILING);
}

// 1.0 if touched within the last month, linear decay to 0 over the next ~23 months.
function recencyScore(unixSeconds: number): number {
  if (unixSeconds <= 0) return 0;
  const ageMonths = (Date.now() / 1000 - unixSeconds) / (60 * 60 * 24 * 30);
  if (ageMonths <= 1) return 1;
  if (ageMonths >= 24) return 0;
  return 1 - (ageMonths - 1) / 23;
}

// log scale: 0 commits = 0, 5+ commits = 1.
function commitDepthScore(count: number): number {
  if (count <= 0) return 0;
  return clamp(Math.log10(count + 1) / Math.log10(6), 0, 1);
}

function payloadTime(r: EvidenceRecord): number {
  const p = r.payload as Record<string, unknown> | null;
  if (!p || typeof p !== 'object') return 0;
  const t = p.author_time;
  return typeof t === 'number' ? t : 0;
}

function clamp(n: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, n));
}
