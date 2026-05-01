import type Database from 'better-sqlite3';
import type { Rationale } from './prompt.js';

export interface RationaleRecord extends Rationale {
  node_id: string;
  bundle_hash: string;
  prompt_version: string;
  model: string;
  confidence: number;
  generated_at: number;
}

interface RawRationaleRow {
  node_id: string;
  bundle_hash: string;
  prompt_version: string;
  model: string;
  purpose: string | null;
  why: string | null;
  constraints: string | null;
  tradeoffs: string | null;
  risks: string | null;
  confidence: number | null;
  generated_at: number;
}

export interface RationaleInput extends Rationale {
  node_id: string;
  bundle_hash: string;
  prompt_version: string;
  model: string;
  confidence: number;
}

export class RationaleStore {
  private readonly getStmt;
  private readonly upsertStmt;

  constructor(db: Database.Database) {
    this.getStmt = db.prepare('SELECT * FROM rationale WHERE node_id = ?');
    this.upsertStmt = db.prepare(`
      INSERT INTO rationale (
        node_id, bundle_hash, prompt_version, model,
        purpose, why, constraints, tradeoffs, risks,
        confidence, generated_at
      ) VALUES (
        @node_id, @bundle_hash, @prompt_version, @model,
        @purpose, @why, @constraints, @tradeoffs, @risks,
        @confidence, @generated_at
      )
      ON CONFLICT(node_id) DO UPDATE SET
        bundle_hash = excluded.bundle_hash,
        prompt_version = excluded.prompt_version,
        model = excluded.model,
        purpose = excluded.purpose,
        why = excluded.why,
        constraints = excluded.constraints,
        tradeoffs = excluded.tradeoffs,
        risks = excluded.risks,
        confidence = excluded.confidence,
        generated_at = excluded.generated_at
    `);
  }

  get(nodeId: string): RationaleRecord | null {
    const row = this.getStmt.get(nodeId) as RawRationaleRow | undefined;
    if (!row) return null;
    return {
      node_id: row.node_id,
      bundle_hash: row.bundle_hash,
      prompt_version: row.prompt_version,
      model: row.model,
      purpose: row.purpose ?? '',
      why: row.why ?? '',
      constraints: parseArray(row.constraints),
      tradeoffs: parseArray(row.tradeoffs),
      risks: parseArray(row.risks),
      confidence: row.confidence ?? 0,
      generated_at: row.generated_at,
    };
  }

  upsert(input: RationaleInput): RationaleRecord {
    const generated_at = Date.now();
    this.upsertStmt.run({
      node_id: input.node_id,
      bundle_hash: input.bundle_hash,
      prompt_version: input.prompt_version,
      model: input.model,
      purpose: input.purpose,
      why: input.why,
      constraints: JSON.stringify(input.constraints),
      tradeoffs: JSON.stringify(input.tradeoffs),
      risks: JSON.stringify(input.risks),
      confidence: input.confidence,
      generated_at,
    });
    return { ...input, generated_at };
  }
}

function parseArray(text: string | null): string[] {
  if (!text) return [];
  try {
    const parsed = JSON.parse(text);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}
