// Read-only view of the shared SQLite store. node:sqlite (built into Node 22+)
// keeps this dependency-free — no native module builds on Windows.
import { DatabaseSync } from 'node:sqlite';
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

export const ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..');

export const DB_PATH =
  process.env.GTL_DB_PATH ?? path.join(ROOT, 'data', 'gtleague.db');

if (!fs.existsSync(DB_PATH)) {
  throw new Error(`database not found at ${DB_PATH} — run an ingest first`);
}

export const db = new DatabaseSync(DB_PATH, { readOnly: true });

export function one<T = Record<string, unknown>>(sql: string, ...args: unknown[]): T | undefined {
  return db.prepare(sql).get(...(args as never[])) as T | undefined;
}

export function all<T = Record<string, unknown>>(sql: string, ...args: unknown[]): T[] {
  return db.prepare(sql).all(...(args as never[])) as T[];
}
