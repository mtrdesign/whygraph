import { execFileSync } from 'node:child_process';
import type { CodeGraphNode } from '../codegraph/reader.js';
import { collectGitEvidence, collectGitHubEvidence } from './collector.js';
import type { GitEvidenceCollector } from './git.js';
import type { GitHubEvidenceCollector } from './github.js';
import type { EvidenceRecord, EvidenceStore } from './store.js';

export interface CollectionResult {
  evidence: EvidenceRecord[];
  bundleHash: string;
  source: 'cache' | 'collected';
  collectedAt: number;
  headAtCollection: string | null;
}

export interface EvidenceServiceOptions {
  ttlMs: number;
}

// Single entry point for "give me current evidence for this node".
// Collects on demand, caches with a freshness check that combines a TTL
// with a per-file HEAD-sha comparison: if the file has new commits since
// the last collection, the cache is considered stale even if still
// within TTL. `force: true` bypasses both checks.
export class EvidenceService {
  private readonly ttlMs: number;

  constructor(
    private readonly evidenceStore: EvidenceStore,
    private readonly git: GitEvidenceCollector,
    private readonly github: GitHubEvidenceCollector | null,
    private readonly repoRoot: string,
    opts: EvidenceServiceOptions
  ) {
    this.ttlMs = opts.ttlMs;
  }

  forNode(node: CodeGraphNode, opts?: { force?: boolean }): CollectionResult {
    if (!opts?.force) {
      const cached = this.checkCache(node);
      if (cached) return cached;
    }
    return this.collect(node);
  }

  private checkCache(node: CodeGraphNode): CollectionResult | null {
    const meta = this.evidenceStore.bundleMetaFor(node.id);
    if (!meta) return null;

    const ageMs = Date.now() - meta.built_at;
    if (ageMs > this.ttlMs) return null;

    if (meta.head_at_collection !== null) {
      const currentHead = this.fileHeadSha(node.file_path);
      if (currentHead === null || currentHead !== meta.head_at_collection) {
        return null;
      }
    }
    // If head_at_collection is null (no git history at collection time)
    // we trust the TTL alone — recollecting on every call would be wasteful.

    return {
      evidence: this.evidenceStore.forNode(node.id),
      bundleHash: meta.bundle_hash,
      source: 'cache',
      collectedAt: meta.built_at,
      headAtCollection: meta.head_at_collection,
    };
  }

  private collect(node: CodeGraphNode): CollectionResult {
    const gitRows = collectGitEvidence(this.git, node);
    const ghRows =
      this.github && this.github.isAvailable()
        ? collectGitHubEvidence(this.github, gitRows)
        : [];
    const rows = gitRows.concat(ghRows);
    const headAtCollection = this.fileHeadSha(node.file_path);
    const bundleHash = this.evidenceStore.replace(
      node.id,
      node.qualified_name,
      rows,
      headAtCollection
    );
    return {
      evidence: this.evidenceStore.forNode(node.id),
      bundleHash,
      source: 'collected',
      collectedAt: Date.now(),
      headAtCollection,
    };
  }

  private fileHeadSha(filePath: string): string | null {
    try {
      const out = execFileSync(
        'git',
        ['log', '-1', '--format=%H', '--', filePath],
        {
          cwd: this.repoRoot,
          encoding: 'utf8',
          stdio: ['ignore', 'pipe', 'pipe'],
        }
      ).trim();
      return out === '' ? null : out;
    } catch {
      return null;
    }
  }
}
