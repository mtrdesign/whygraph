import Database from 'better-sqlite3';
import { mkdirSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const SCHEMA_PATH = resolve(here, 'schema.sql');

export function openWhyGraphDb(path: string): Database.Database {
  mkdirSync(dirname(path), { recursive: true });
  const db = new Database(path);
  db.pragma('journal_mode = WAL');
  db.pragma('foreign_keys = ON');
  db.exec(readFileSync(SCHEMA_PATH, 'utf8'));
  migrate(db);
  return db;
}

interface ColumnInfo {
  name: string;
}

// Apply small column-add migrations for DBs created by older versions.
// CREATE TABLE IF NOT EXISTS doesn't add new columns to an existing table,
// so we detect missing columns via PRAGMA table_info and ALTER as needed.
function migrate(db: Database.Database): void {
  const cols = db
    .prepare('PRAGMA table_info(evidence_bundles)')
    .all() as ColumnInfo[];
  if (!cols.some((c) => c.name === 'head_at_collection')) {
    db.exec('ALTER TABLE evidence_bundles ADD COLUMN head_at_collection TEXT');
  }
}
