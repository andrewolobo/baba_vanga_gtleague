import Fastify from 'fastify';
import fastifyStatic from '@fastify/static';
import path from 'node:path';
import { ROOT } from './db.js';
import { registerRoutes } from './routes.js';
import { registerSse } from './sse.js';
import { startScheduler } from './jobs.js';

const PORT = Number(process.env.PORT ?? 8787);

const app = Fastify({ logger: { level: 'warn' } });

app.register(fastifyStatic, {
  root: path.join(ROOT, 'apps', 'web', 'public'),
});
registerRoutes(app);
registerSse(app);

app.listen({ port: PORT, host: '127.0.0.1' }).then(() => {
  console.log(`gtleague api+ui on http://localhost:${PORT}`);
  if (process.env.GTL_NO_JOBS !== '1') {
    void startScheduler(); // dev mode: the API is the orchestrator (Phase 5 note)
  } else {
    console.log('scheduler disabled (GTL_NO_JOBS=1)');
  }
});
