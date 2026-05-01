import type Database from 'better-sqlite3';
import { createHash } from 'node:crypto';

export type EvidenceSource =
  | 'git_blame'
  | 'git_commit'
  | 'pr'
  | 'issue'
  | 'docstring'
  | 'test_ref';

export interface EvidenceRow {
  source: EvidenceSource;
  ref: string | null;
  payload: unknown;
}

export interface EvidenceRecord {
  id: number;
  node_id: string;
  qualified_name: string;
  source: EvidenceSource;
  ref: string | null;
  payload: unknown;
  collected_at: number;
}

interface RawEvidenceRow {
  id: number;
  node_id: string;
  qualified_name: string;
  source: string;
  ref: string | null;
  payload: string;
  collected_at: number;
}

export class EvidenceStore {
  private readonly insertStmt;
  private readonly purgeStmt;
  private readonly selectByNode;
  private readonly upsertBundle;

  constructor(private readonly db: Database.Database) {
    this.insertStmt = db.prepare(`
      INSERT INTO evidence (node_id, qualified_name, source, ref, payload, collected_at)
      VALUES (@node_id, @qualified_name, @source, @ref, @payload, @collected_at)
      ON CONFLICT(node_id, source, ref) DO UPDATE SET
        payload = excluded.payload,
        qualified_name = excluded.qualified_name,
        collected_at = excluded.collected_at
    `);
    this.purgeStmt = db.prepare('DELETE FROM evidence WHERE node_id = ?');
    this.selectByNode = db.prepare(
      'SELECT * FROM evidence WHERE node_id = ? ORDER BY id'
    );
    this.upsertBundle = db.prepare(`
      INSERT INTO evidence_bundles (node_id, bundle_hash, built_at)
      VALUES (?, ?, ?)
      ON CONFLICT(node_id) DO UPDATE SET
        bundle_hash = excluded.bundle_hash,
        built_at = excluded.built_at
    `);
  }

  // Replaces all evidence for a node atomically. Returns the new bundle hash.
  replace(nodeId: string, qualifiedName: string, rows: EvidenceRow[]): string {
    const now = Date.now();
    const tx = this.db.transaction(() => {
      this.purgeStmt.run(nodeId);
      for (const row of rows) {
        this.insertStmt.run({
          node_id: nodeId,
          qualified_name: qualifiedName,
          source: row.source,
          ref: row.ref,
          payload: JSON.stringify(row.payload),
          collected_at: now,
        });
      }
      const hash = computeBundleHash(rows);
      this.upsertBundle.run(nodeId, hash, now);
      return hash;
    });
    return tx();
  }

  bundleHashFor(nodeId: string): string | null {
    const row = this.db
      .prepare('SELECT bundle_hash FROM evidence_bundles WHERE node_id = ?')
      .get(nodeId) as { bundle_hash: string } | undefined;
    return row?.bundle_hash ?? null;
  }

  forNode(nodeId: string): EvidenceRecord[] {
    const rows = this.selectByNode.all(nodeId) as RawEvidenceRow[];
    return rows.map((r) => ({
      id: r.id,
      node_id: r.node_id,
      qualified_name: r.qualified_name,
      source: r.source as EvidenceSource,
      ref: r.ref,
      payload: parseJsonSafe(r.payload),
      collected_at: r.collected_at,
    }));
  }
}

export function computeBundleHash(rows: EvidenceRow[]): string {
  const sorted = [...rows].sort((a, b) => {
    const ka = `${a.source}|${a.ref ?? ''}`;
    const kb = `${b.source}|${b.ref ?? ''}`;
    return ka < kb ? -1 : ka > kb ? 1 : 0;
  });
  const h = createHash('sha256');
  for (const r of sorted) {
    h.update(r.source);
    h.update('|');
    h.update(r.ref ?? '');
    h.update('|');
    h.update(stableStringify(r.payload));
    h.update('\n');
  }
  return h.digest('hex');
}

function parseJsonSafe(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function stableStringify(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) {
    return '[' + value.map(stableStringify).join(',') + ']';
  }
  const obj = value as Record<string, unknown>;
  const keys = Object.keys(obj).sort();
  return (
    '{' +
    keys
      .map((k) => JSON.stringify(k) + ':' + stableStringify(obj[k]))
      .join(',') +
    '}'
  );
}
