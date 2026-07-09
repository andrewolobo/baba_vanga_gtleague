// Server-Sent Events: push "something changed" so the UI never polls.
// The watcher polls SQLite cheaply in-process; clients get one small event.
import type { FastifyInstance } from 'fastify';
import { one } from './db.js';

interface Client { write: (chunk: string) => void }
const clients = new Set<Client>();

function snapshot() {
  return {
    predictions: one<{ v: number }>('SELECT COALESCE(MAX(id),0) v FROM predictions')?.v ?? 0,
    settlements: one<{ v: number }>('SELECT COUNT(*) v FROM settlements')?.v ?? 0,
    odds: one<{ v: string }>("SELECT COALESCE(MAX(fetched_at),'') v FROM odds_snapshots")?.v ?? '',
  };
}

export function registerSse(app: FastifyInstance): void {
  let last = JSON.stringify(snapshot());

  setInterval(() => {
    const cur = JSON.stringify(snapshot());
    if (cur !== last) {
      last = cur;
      for (const c of clients) c.write(`event: update\ndata: ${cur}\n\n`);
    }
  }, 3_000).unref();

  setInterval(() => { // keep-alive
    for (const c of clients) c.write(': ping\n\n');
  }, 25_000).unref();

  app.get('/api/events', (req, reply) => {
    reply.raw.writeHead(200, {
      'content-type': 'text/event-stream',
      'cache-control': 'no-cache',
      connection: 'keep-alive',
    });
    reply.raw.write(`event: update\ndata: ${last}\n\n`);
    const client: Client = { write: (s) => reply.raw.write(s) };
    clients.add(client);
    req.raw.on('close', () => clients.delete(client));
  });
}
