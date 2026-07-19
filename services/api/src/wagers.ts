// Paper-trading (wagers) surface — the ONE write path in this API. Everything
// in routes.ts is a read of model output; this file records USER state: the
// hypothetical slips a viewer places against the book-priced slate. It writes
// only its own wagers/wager_legs tables (never a model table) through db.ts's
// `run`/`dbw`.
//
// Grading is DERIVED at read time (join settlements/settlements_x12), never
// stored: a wager is frozen at placement (stake + per-leg odds snapshot) and
// its result follows whatever the Python settlement loops later record. Only
// book-priced fixtures are bettable — schedule-only gtl: events never settle,
// so POST rejects them before a row exists.
import type { FastifyInstance } from 'fastify';
import { all, one, run } from './db.js';

const CLUB = (raw: string) => raw.replace(/\s*\([^)]*\)\s*$/, '');
const PLAYER = (raw: string) => (raw.match(/\(([^)]*)\)/) ?? [])[1] ?? null;
// same now-2min grace the slate uses: a fixture is bettable/cancellable until
// two minutes past its scheduled kickoff
const graceNow = () => new Date(Date.now() - 2 * 60_000).toISOString();

const OU_SEL = new Set(['over', 'under']);
const X12_SEL = new Set(['home', 'draw', 'away']);

interface WagerRow { id: number; placed_at: string; stake: number; total_odds: number }
interface LegRow {
  id: number; wager_id: number; event_id: string; market: string;
  line: number | null; selection: string; odds: number;
}
type LegOutcome = 'pending' | 'won' | 'lost' | 'push' | 'void';
type WagerStatus = 'open' | 'won' | 'lost';

// Derived grade for one leg. Cancelled match (status 4) voids the leg (odds
// 1.0); no settlement row yet = pending; otherwise the totals/1x2 result
// decides it. Never reads a stored grade — there is none.
function legOutcome(leg: LegRow): LegOutcome {
  if (leg.market === '1x2') {
    const s = one<{ result: string; match_status: number | null }>(
      `SELECT s.result, m.status AS match_status FROM settlements_x12 s
       LEFT JOIN matches m ON m.id = s.matched_match_id WHERE s.event_id = ?`,
      leg.event_id);
    if (!s) return 'pending';
    if (s.match_status === 4) return 'void';
    return s.result === leg.selection ? 'won' : 'lost';
  }
  const s = one<{ result_total: number | null; match_status: number | null }>(
    `SELECT s.result_total, m.status AS match_status FROM settlements s
     LEFT JOIN matches m ON m.id = s.matched_match_id WHERE s.event_id = ?`,
    leg.event_id);
  if (!s) return 'pending';
  if (s.match_status === 4) return 'void';
  if (s.result_total == null) return 'pending';
  if (s.result_total === leg.line) return 'push';
  const overWon = s.result_total > (leg.line ?? 0);
  const won = leg.selection === 'over' ? overWon : !overWon;
  return won ? 'won' : 'lost';
}

// Parlay resolution: one lost leg kills the slip; any pending leg (with none
// lost) leaves it open; otherwise it won — pushes/voids ride along at 1.0.
function wagerStatus(outcomes: LegOutcome[]): WagerStatus {
  if (outcomes.includes('lost')) return 'lost';
  if (outcomes.includes('pending')) return 'open';
  return 'won';
}

// payout = stake × product(leg odds), push/void legs counted as 1.0. A won
// wager with only pushes/voids returns exactly the stake. Open pays null.
function payoutOf(status: WagerStatus, stake: number, legs: LegRow[],
                  outcomes: LegOutcome[]): number | null {
  if (status === 'lost') return 0;
  if (status === 'open') return null;
  const prod = legs.reduce((a, leg, i) =>
    a * (outcomes[i] === 'won' ? leg.odds : 1.0), 1);
  return Math.round(stake * prod * 100) / 100;
}

// Shape a wager + its legs for the API, grading each leg and the slip. Carries
// per-leg display fields (clubs/players/kickoff) so the UI needn't join.
function shapeWager(w: WagerRow) {
  const legRows = all<LegRow>(
    'SELECT * FROM wager_legs WHERE wager_id = ? ORDER BY id', w.id);
  const outcomes = legRows.map(legOutcome);
  const status = wagerStatus(outcomes);
  const legs = legRows.map((leg, i) => {
    const fx = one<{ start_time_utc: string; home_raw: string; away_raw: string }>(
      'SELECT start_time_utc, home_raw, away_raw FROM fixtures WHERE event_id = ?',
      leg.event_id);
    return {
      event_id: leg.event_id, market: leg.market, line: leg.line,
      selection: leg.selection, odds: leg.odds, outcome: outcomes[i],
      kickoff: fx?.start_time_utc ?? null,
      home_club: fx ? CLUB(fx.home_raw) : null,
      away_club: fx ? CLUB(fx.away_raw) : null,
      home_player: fx ? PLAYER(fx.home_raw) : null,
      away_player: fx ? PLAYER(fx.away_raw) : null,
    };
  });
  return {
    id: w.id, placed_at: w.placed_at, stake: w.stake,
    total_odds: w.total_odds, status,
    payout: payoutOf(status, w.stake, legRows, outcomes),
    legs,
  };
}

function loadWager(id: number) {
  const w = one<WagerRow>('SELECT * FROM wagers WHERE id = ?', id);
  return w ? shapeWager(w) : null;
}

// Latest snapshotted book odds for a leg — the server's price, never trusted
// from the client. 1x2 lines are null in the store, so don't filter on line.
function latestLegOdds(market: string, line: number | null, selection: string,
                       eventId: string): number | null {
  const r = market === '1x2'
    ? one<{ odds: number }>(
        `SELECT odds FROM odds_snapshots WHERE event_id = ? AND market = '1x2'
         AND selection = ? ORDER BY fetched_at DESC LIMIT 1`, eventId, selection)
    : one<{ odds: number }>(
        `SELECT odds FROM odds_snapshots WHERE event_id = ? AND market = 'ou'
         AND line = ? AND selection = ? ORDER BY fetched_at DESC LIMIT 1`,
        eventId, line, selection);
  return r?.odds ?? null;
}

interface LegInput { event_id?: unknown; market?: unknown; line?: unknown; selection?: unknown }

export function registerWagerRoutes(app: FastifyInstance): void {
  // Newest first, each fully graded at read time.
  app.get('/api/wagers', async () =>
    all<WagerRow>('SELECT * FROM wagers ORDER BY placed_at DESC, id DESC')
      .map(shapeWager));

  app.post('/api/wagers', async (req, reply) => {
    const body = (req.body ?? {}) as { stake?: unknown; legs?: unknown };
    const stake = Number(body.stake);
    if (!Number.isFinite(stake) || stake <= 0) {
      return reply.code(400).send({ error: 'stake must be a positive number' });
    }
    if (!Array.isArray(body.legs) || body.legs.length === 0) {
      return reply.code(400).send({ error: 'at least one leg is required' });
    }

    const seen = new Set<string>();
    const now = graceNow();
    const resolved: { event_id: string; market: string; line: number | null;
                      selection: string; odds: number }[] = [];

    for (const raw of body.legs as LegInput[]) {
      const event_id = String((raw?.event_id ?? '')).trim();
      const market = String(raw?.market ?? '');
      const selection = String(raw?.selection ?? '');
      if (!event_id) return reply.code(400).send({ error: 'each leg needs an event_id' });
      if (event_id.startsWith('gtl:')) {
        return reply.code(400).send(
          { error: `schedule-only event ${event_id} is not bettable (never settles)` });
      }
      if (seen.has(event_id)) {
        return reply.code(400).send(
          { error: `duplicate event ${event_id} — one leg per event` });
      }
      seen.add(event_id);
      if (market !== 'ou' && market !== '1x2') {
        return reply.code(400).send({ error: `bad market '${market}'` });
      }
      const validSel = market === 'ou' ? OU_SEL.has(selection) : X12_SEL.has(selection);
      if (!validSel) {
        return reply.code(400).send(
          { error: `bad selection '${selection}' for market ${market}` });
      }
      let line: number | null = null;
      if (market === 'ou') {
        line = Number(raw?.line);
        if (!Number.isFinite(line)) {
          return reply.code(400).send({ error: 'o/u leg needs a numeric line' });
        }
      }
      const fx = one<{ start_time_utc: string }>(
        'SELECT start_time_utc FROM fixtures WHERE event_id = ?', event_id);
      if (!fx) {
        return reply.code(400).send({ error: `unknown fixture ${event_id}` });
      }
      if (fx.start_time_utc <= now) {
        return reply.code(400).send(
          { error: `event ${event_id} has already kicked off` });
      }
      const odds = latestLegOdds(market, line, selection, event_id);
      if (odds == null) {
        return reply.code(400).send(
          { error: `no book odds for ${event_id} ${market} ${selection}${
            line != null ? ' ' + line : ''}` });
      }
      resolved.push({ event_id, market, line, selection, odds });
    }

    const totalOdds = Math.round(
      resolved.reduce((a, l) => a * l.odds, 1) * 1000) / 1000;
    const placedAt = new Date().toISOString();

    run('BEGIN');
    let wagerId: number;
    try {
      const w = run('INSERT INTO wagers (placed_at, stake, total_odds) VALUES (?, ?, ?)',
        placedAt, stake, totalOdds);
      wagerId = Number(w.lastInsertRowid);
      for (const l of resolved) {
        run(`INSERT INTO wager_legs (wager_id, event_id, market, line, selection, odds)
             VALUES (?, ?, ?, ?, ?, ?)`,
          wagerId, l.event_id, l.market, l.line, l.selection, l.odds);
      }
      run('COMMIT');
    } catch (e) {
      run('ROLLBACK');
      throw e;
    }
    return reply.code(201).send(loadWager(wagerId));
  });

  // Summary over the derived grades. Only SETTLED wagers feed staked/returned/
  // roi; open volume is reported separately. A slip with no won leg but no lost
  // leg (all push/void) is a push — returns its stake.
  app.get('/api/wagers/summary', async () => {
    const wagers = all<WagerRow>('SELECT * FROM wagers').map(shapeWager);
    const bucket = (xs: ReturnType<typeof shapeWager>[]) => {
      const rec = { won: 0, lost: 0, open: 0, push: 0 };
      let staked = 0, returned = 0, openStake = 0;
      for (const w of xs) {
        if (w.status === 'open') { rec.open += 1; openStake += w.stake; continue; }
        if (w.status === 'lost') { rec.lost += 1; staked += w.stake; continue; }
        // won by resolution: distinguish a real win from an all-push/void slip
        const isPush = (w.payout ?? 0) === w.stake
          && !w.legs.some((l) => l.outcome === 'won');
        if (isPush) rec.push += 1; else rec.won += 1;
        staked += w.stake;
        returned += w.payout ?? 0;
      }
      const pnl = Math.round((returned - staked) * 100) / 100;
      return {
        record: rec, staked: Math.round(staked * 100) / 100,
        returned: Math.round(returned * 100) / 100, pnl,
        roi: staked > 0 ? Math.round((pnl / staked) * 1000) / 1000 : null,
        open_stake: Math.round(openStake * 100) / 100,
      };
    };
    return {
      ...bucket(wagers),
      singles: bucket(wagers.filter((w) => w.legs.length === 1)),
      parlays: bucket(wagers.filter((w) => w.legs.length > 1)),
    };
  });

  // Cancellable only while EVERY leg's fixture is still in the future — once
  // any leg has kicked off the slip is locked (409). 404 for an unknown id.
  app.delete<{ Params: { id: string } }>('/api/wagers/:id', async (req, reply) => {
    const id = Number(req.params.id);
    const w = one<WagerRow>('SELECT * FROM wagers WHERE id = ?', id);
    if (!w) return reply.code(404).send({ error: 'no such wager' });
    const now = graceNow();
    const started = one<{ n: number }>(
      `SELECT COUNT(*) AS n FROM wager_legs wl
       JOIN fixtures f ON f.event_id = wl.event_id
       WHERE wl.wager_id = ? AND f.start_time_utc <= ?`, id, now);
    if ((started?.n ?? 0) > 0) {
      return reply.code(409).send(
        { error: 'cannot cancel — a leg has already kicked off' });
    }
    run('BEGIN');
    try {
      run('DELETE FROM wager_legs WHERE wager_id = ?', id);
      run('DELETE FROM wagers WHERE id = ?', id);
      run('COMMIT');
    } catch (e) {
      run('ROLLBACK');
      throw e;
    }
    return { ok: true, id };
  });
}
