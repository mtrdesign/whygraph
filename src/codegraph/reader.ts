import Database from 'better-sqlite3';
import { existsSync, statSync } from 'node:fs';
import { dirname, join, parse, resolve } from 'node:path';

const CODEGRAPH_DIR = '.codegraph';
const CODEGRAPH_DB_FILE = 'codegraph.db';

export interface CodeGraphNode {
  id: string;
  kind: string;
  name: string;
  qualified_name: string;
  file_path: string;
  language: string;
  start_line: number;
  end_line: number;
  start_column: number;
  end_column: number;
  docstring: string | null;
  signature: string | null;
  visibility: string | null;
  is_exported: number;
  is_async: number;
  is_static: number;
  is_abstract: number;
  decorators: string | null;
  type_parameters: string | null;
  updated_at: number;
}

export interface CodeGraphEdge {
  id: number;
  source: string;
  target: string;
  kind: string;
  metadata: string | null;
  line: number | null;
  col: number | null;
  provenance: string | null;
}

export interface CodeGraphFile {
  path: string;
  content_hash: string;
  language: string;
  size: number;
  modified_at: number;
  indexed_at: number;
  node_count: number;
  errors: string | null;
}

// Walks up from startPath looking for `.codegraph/codegraph.db`, mirroring
// CodeGraph's own discovery behaviour (see codegraph/src/directory.ts).
export function findCodeGraphDb(startPath: string): string | null {
  let current = resolve(startPath);
  const root = parse(current).root;
  while (true) {
    const dbPath = join(current, CODEGRAPH_DIR, CODEGRAPH_DB_FILE);
    if (existsSync(dbPath) && statSync(dbPath).isFile()) {
      return dbPath;
    }
    if (current === root) return null;
    const parent = dirname(current);
    if (parent === current) return null;
    current = parent;
  }
}

interface NodeFilter {
  kinds?: string[];
  language?: string;
  filePath?: string;
}

export class CodeGraphReader {
  private db: Database.Database;

  constructor(path: string) {
    this.db = new Database(path, { readonly: true, fileMustExist: true });
  }

  close(): void {
    this.db.close();
  }

  countNodes(): number {
    return (this.db.prepare('SELECT COUNT(*) AS n FROM nodes').get() as { n: number }).n;
  }

  countEdges(): number {
    return (this.db.prepare('SELECT COUNT(*) AS n FROM edges').get() as { n: number }).n;
  }

  countFiles(): number {
    return (this.db.prepare('SELECT COUNT(*) AS n FROM files').get() as { n: number }).n;
  }

  languageBreakdown(): Array<{ language: string; nodes: number }> {
    return this.db
      .prepare(
        'SELECT language, COUNT(*) AS nodes FROM nodes GROUP BY language ORDER BY nodes DESC'
      )
      .all() as Array<{ language: string; nodes: number }>;
  }

  kindBreakdown(): Array<{ kind: string; nodes: number }> {
    return this.db
      .prepare('SELECT kind, COUNT(*) AS nodes FROM nodes GROUP BY kind ORDER BY nodes DESC')
      .all() as Array<{ kind: string; nodes: number }>;
  }

  *iterateNodes(filter?: NodeFilter): IterableIterator<CodeGraphNode> {
    let sql = 'SELECT * FROM nodes';
    const where: string[] = [];
    const params: unknown[] = [];
    if (filter?.kinds?.length) {
      where.push(`kind IN (${filter.kinds.map(() => '?').join(',')})`);
      params.push(...filter.kinds);
    }
    if (filter?.language) {
      where.push('language = ?');
      params.push(filter.language);
    }
    if (filter?.filePath) {
      where.push('file_path = ?');
      params.push(filter.filePath);
    }
    if (where.length) sql += ' WHERE ' + where.join(' AND ');
    sql += ' ORDER BY file_path, start_line';
    yield* this.db.prepare(sql).iterate(...params) as IterableIterator<CodeGraphNode>;
  }

  getNode(id: string): CodeGraphNode | undefined {
    return this.db.prepare('SELECT * FROM nodes WHERE id = ?').get(id) as
      | CodeGraphNode
      | undefined;
  }

  findNodesByQualifiedName(qualifiedName: string): CodeGraphNode[] {
    return this.db
      .prepare('SELECT * FROM nodes WHERE qualified_name = ?')
      .all(qualifiedName) as CodeGraphNode[];
  }

  edgesFrom(nodeId: string, kind?: string): CodeGraphEdge[] {
    const sql = kind
      ? 'SELECT * FROM edges WHERE source = ? AND kind = ?'
      : 'SELECT * FROM edges WHERE source = ?';
    const params = kind ? [nodeId, kind] : [nodeId];
    return this.db.prepare(sql).all(...params) as CodeGraphEdge[];
  }

  edgesTo(nodeId: string, kind?: string): CodeGraphEdge[] {
    const sql = kind
      ? 'SELECT * FROM edges WHERE target = ? AND kind = ?'
      : 'SELECT * FROM edges WHERE target = ?';
    const params = kind ? [nodeId, kind] : [nodeId];
    return this.db.prepare(sql).all(...params) as CodeGraphEdge[];
  }

  getFile(path: string): CodeGraphFile | undefined {
    return this.db.prepare('SELECT * FROM files WHERE path = ?').get(path) as
      | CodeGraphFile
      | undefined;
  }
}
