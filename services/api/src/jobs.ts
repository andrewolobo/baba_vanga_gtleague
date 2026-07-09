// Embedded dev scheduler: booting the API spins up the whole pipeline
// (the user-requested Phase-7 shape — a standalone scheduler comes later).
// Each job is a short-lived Python CLI spawned from the repo venv; jobs never
// overlap themselves, and completions chain: ingest -> predict, results -> settle.
import { spawn } from 'node:child_process';
import path from 'node:path';
import { ROOT, one } from './db.js';

const PYTHON = process.env.GTL_PYTHON
  ?? path.join(ROOT, '.venv', 'Scripts', 'python.exe');

const MIN = 60_000;

type JobName = 'catchup' | 'results' | 'odds' | 'predict' | 'settle' | 'backup';

interface JobSpec {
  args: string[];
  everyMs: number;
  onSuccess: JobName[];
}

const SPECS: Record<JobName, JobSpec> = {
  catchup: { args: [], everyMs: 0, onSuccess: [] }, // args computed at boot
  results: { args: ['-m', 'results_ingest.cli', 'today', '--no-archive'],
             everyMs: 10 * MIN, onSuccess: ['predict', 'settle'] },
  odds:    { args: ['-m', 'odds_ingest.cli', 'fetch'],
             everyMs: 5 * MIN, onSuccess: ['predict'] },
  predict: { args: ['-m', 'predictor.cycle'], everyMs: 0, onSuccess: [] },
  settle:  { args: ['-m', 'settlement.settle', 'run'], everyMs: 15 * MIN,
             onSuccess: [] },
  backup:  { args: ['-m', 'store.backup'], everyMs: 24 * 60 * MIN,
             onSuccess: [] },
};

/** Days of finished results missing from the DB (0 = current). Downtime
 *  leaves a hole the `today` job never re-fetches; the catch-up closes it. */
export function resultsGapDays(): number {
  const last = one<{ d: string | null }>(
    'SELECT MAX(date) d FROM matches WHERE status = 3')?.d;
  if (!last) return 0;
  return Math.max(0, Math.floor(
    (Date.now() - Date.parse(`${last}T00:00:00Z`)) / 86_400_000));
}

export interface JobStatus {
  running: boolean;
  lastStart: string | null;
  lastExit: number | null;
  lastMs: number | null;
  lastLine: string;
  runs: number;
  failures: number;
}

export const jobStatus: Record<JobName, JobStatus> = Object.fromEntries(
  Object.keys(SPECS).map((n) => [n, {
    running: false, lastStart: null, lastExit: null, lastMs: null,
    lastLine: '', runs: 0, failures: 0,
  }]),
) as Record<JobName, JobStatus>;

export function runJob(name: JobName, argsOverride?: string[]): Promise<number> {
  const st = jobStatus[name];
  if (st.running) return Promise.resolve(-1); // never overlap the same job
  st.running = true;
  st.lastStart = new Date().toISOString();
  const t0 = Date.now();

  return new Promise((resolve) => {
    const child = spawn(PYTHON, argsOverride ?? SPECS[name].args, { cwd: ROOT });
    let tail = '';
    const capture = (b: Buffer) => {
      tail = (tail + b.toString()).split(/\r?\n/).filter(Boolean).slice(-3).join(' | ');
    };
    child.stdout.on('data', capture);
    child.stderr.on('data', capture);
    child.on('error', (err) => { tail = String(err); });
    child.on('close', (code) => {
      st.running = false;
      st.lastExit = code ?? -1;
      st.lastMs = Date.now() - t0;
      st.lastLine = tail.slice(0, 400);
      st.runs += 1;
      // exit 2 = data-quality warning (rows still land) — not a failure
      if (code !== 0 && code !== 2) st.failures += 1;
      console.log(`[job ${name}] exit=${code} ${st.lastMs}ms :: ${st.lastLine}`);
      if (code === 0 || code === 2) {
        for (const next of SPECS[name].onSuccess) void runJob(next);
      }
      resolve(code ?? -1);
    });
  });
}

export async function startScheduler(): Promise<void> {
  // catch-up first: downtime > 1 day leaves result days the `today` job never
  // re-fetches, and the day's Poisson would silently train without them
  // (7-day half-life makes those the heaviest rows). The registry refits
  // automatically once the backfilled rows change its data fingerprint.
  const gap = resultsGapDays();
  if (gap > 1) {
    const from = new Date(Date.now() - gap * 86_400_000)
      .toISOString().slice(0, 10);
    const to = new Date().toISOString().slice(0, 10);
    console.log(`[job catchup] results gap of ${gap}d — backfilling ${from}..${to}`);
    await runJob('catchup',
      ['-m', 'results_ingest.cli', 'backfill', '--from', from, '--to', to]);
  }
  // boot sequence: results first (freshest form), then odds -> predict -> settle
  await runJob('results');
  await runJob('odds');
  await runJob('settle');
  await runJob('backup');
  for (const name of ['results', 'odds', 'settle', 'backup'] as JobName[]) {
    setInterval(() => void runJob(name), SPECS[name].everyMs).unref();
  }
}
