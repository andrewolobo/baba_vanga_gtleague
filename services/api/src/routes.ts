// Read-only API. Every route is a SQL read + reshaping — no computation of
// picks here (picks are server-derived in Python and persisted; the UI and
// this API are dumb renderers of the predictions table).
import type { FastifyInstance } from 'fastify';
import { all, one } from './db.js';
import { jobStatus, resultsGapDays } from './jobs.js';

const CLUB = (raw: string) => raw.replace(/\s*\([^)]*\)\s*$/, '');

interface PredRow {
  event_id: string; predicted_at: string; totals_source: string; line: number;
  p_over: number; p_push: number; p_under: number;
  lambda_home: number | null; lambda_away: number | null;
  pick: string | null; confidence: number | null; tier: string | null;
  value_flag: number; model_version: string;
}

function latestPredictions(eventId: string): PredRow[] {
  return all<PredRow>(
    `SELECT * FROM predictions WHERE event_id = ? AND predicted_at =
       (SELECT MAX(predicted_at) FROM predictions WHERE event_id = ?)
     ORDER BY line`, eventId, eventId);
}

function latestOdds(eventId: string) {
  const rows = all<{ market: string; line: number | null; selection: string;
                     odds: number; implied_prob: number | null }>(
    `SELECT market, line, selection, odds, implied_prob FROM odds_snapshots
     WHERE event_id = ? AND fetched_at =
       (SELECT MAX(fetched_at) FROM odds_snapshots WHERE event_id = ?)`,
    eventId, eventId);
  const ou: Record<string, Record<string, { odds: number; implied: number | null }>> = {};
  const x12: Record<string, { odds: number; implied: number | null }> = {};
  for (const r of rows) {
    if (r.market === 'ou' && r.line != null) {
      (ou[r.line] ??= {})[r.selection] = { odds: r.odds, implied: r.implied_prob };
    } else if (r.market === '1x2') {
      x12[r.selection] = { odds: r.odds, implied: r.implied_prob };
    }
  }
  return { ou, x12 };
}

export function registerRoutes(app: FastifyInstance): void {
  app.get('/api/slate', async () => {
    const now = new Date(Date.now() - 2 * 60_000).toISOString();
    const fixtures = all<{ event_id: string; start_time_utc: string;
      competition: string; home_raw: string; away_raw: string;
      home_player: string | null; away_player: string | null; coverage: string }>(
      'SELECT * FROM fixtures WHERE start_time_utc > ? ORDER BY start_time_utc',
      now);

    const priced = fixtures.map((f) => {
      const preds = latestPredictions(f.event_id);
      const odds = latestOdds(f.event_id);
      const lines = preds.map((p) => {
        const o = odds.ou[p.line] ?? {};
        const bookOver = o.over?.implied ?? null;
        const bookUnder = o.under?.implied ?? null;
        // edge is for the model-favored side, pick or not (value can exist
        // on lines below the pick gates)
        const side = p.p_over > p.p_under ? 'over' : 'under';
        const bookPick = side === 'over' ? bookOver : bookUnder;
        const modelPick = side === 'over' ? p.p_over : p.p_under;
        return {
          line: p.line, p_over: p.p_over, p_push: p.p_push, p_under: p.p_under,
          pick: p.pick, tier: p.tier, confidence: p.confidence,
          value: !!p.value_flag, totals_source: p.totals_source,
          over_odds: o.over?.odds ?? null, under_odds: o.under?.odds ?? null,
          book_over: bookOver, book_under: bookUnder,
          edge: modelPick != null && bookPick != null
            ? Math.round((modelPick - bookPick) * 1000) / 1000 : null,
        };
      });
      return {
        event_id: f.event_id,
        kickoff: f.start_time_utc,
        competition: f.competition,
        home: { raw: f.home_raw, club: CLUB(f.home_raw), player: f.home_player },
        away: { raw: f.away_raw, club: CLUB(f.away_raw), player: f.away_player },
        coverage: f.coverage,
        priced: true,
        lambda_home: preds[0]?.lambda_home ?? null,
        lambda_away: preds[0]?.lambda_away ?? null,
        model_version: preds[0]?.model_version ?? null,
        predicted_at: preds[0]?.predicted_at ?? null,
        lines,
      };
    });

    // league-scheduled games the book hasn't priced yet (model-only cards)
    const taken = new Set(fixtures.map(
      (f) => `${f.start_time_utc}|${f.home_player}|${f.away_player}`));
    const sched = all<{ source_match_id: string; kickoff_ts: string;
      competition: string; home_player: string; away_player: string;
      home_club: string; away_club: string }>(
      'SELECT * FROM matches WHERE status = 0 AND kickoff_ts > ? ORDER BY kickoff_ts',
      now);
    const unpriced = sched
      .filter((m) => !taken.has(`${m.kickoff_ts}|${m.home_player}|${m.away_player}`))
      .map((m) => {
        const preds = latestPredictions(`gtl:${m.source_match_id}`);
        if (!preds.length) return null;
        return {
          event_id: `gtl:${m.source_match_id}`,
          kickoff: m.kickoff_ts,
          competition: m.competition,
          home: { raw: `${m.home_club} (${m.home_player})`, club: m.home_club,
                  player: m.home_player },
          away: { raw: `${m.away_club} (${m.away_player})`, club: m.away_club,
                  player: m.away_player },
          coverage: 'full',
          priced: false,
          lambda_home: preds[0].lambda_home,
          lambda_away: preds[0].lambda_away,
          model_version: preds[0].model_version,
          predicted_at: preds[0].predicted_at,
          lines: preds.map((p) => ({
            line: p.line, p_over: p.p_over, p_push: p.p_push, p_under: p.p_under,
            pick: p.pick, tier: p.tier, confidence: p.confidence,
            value: false, totals_source: p.totals_source,
            over_odds: null, under_odds: null, book_over: null,
            book_under: null, edge: null,
          })),
        };
      })
      .filter((x) => x != null);

    return [...priced, ...unpriced]
      .sort((a, b) => a.kickoff.localeCompare(b.kickoff));
  });

  app.get<{ Querystring: { days?: string } }>('/api/metrics', async (req) => {
    const days = Math.min(Number(req.query.days ?? 7) || 7, 90);
    const since = new Date(Date.now() - days * 86_400_000).toISOString();
    // headline row per settled event = highest-confidence picked line of the
    // last batch served before kickoff (same rule as settlement grading)
    const rows = all<{ event_id: string; pick_correct: number | null;
      leak_risk: number; regen_agrees: number | null; tier: string | null;
      pick: string | null; value_flag: number; p_over: number; line: number;
      result_total: number }>(
      `SELECT s.event_id, s.pick_correct, s.leak_risk, s.regen_agrees,
              s.result_total, p.tier, p.pick, p.value_flag, p.p_over, p.line
       FROM settlements s
       JOIN fixtures f ON f.event_id = s.event_id
       JOIN predictions p ON p.event_id = s.event_id AND p.predicted_at =
         (SELECT MAX(predicted_at) FROM predictions
          WHERE event_id = s.event_id AND predicted_at < f.start_time_utc)
       WHERE s.settled_at >= ?
       ORDER BY s.event_id, (p.pick IS NULL), p.confidence DESC`, since);

    const seen = new Set<string>();
    const headline = rows.filter((r) =>
      seen.has(r.event_id) ? false : (seen.add(r.event_id), true));
    const graded = headline.filter((r) => r.pick_correct != null);
    const hit = (xs: typeof graded) =>
      xs.length ? xs.reduce((a, r) => a + (r.pick_correct ?? 0), 0) / xs.length : null;
    const brier = graded.length
      ? graded.reduce((a, r) =>
          a + (r.p_over - (r.result_total > r.line ? 1 : 0)) ** 2, 0) / graded.length
      : null;
    const regen = headline.filter((r) => r.regen_agrees != null);

    return {
      window_days: days,
      settled: headline.length,
      graded: graded.length,
      hit_rate: hit(graded),
      tiers: Object.fromEntries(['strong', 'solid', 'lean'].map((t) => {
        const xs = graded.filter((r) => r.tier === t);
        return [t, { n: xs.length, hit_rate: hit(xs) }];
      })),
      value: (() => {
        const xs = graded.filter((r) => r.value_flag === 1);
        return { n: xs.length, hit_rate: hit(xs) };
      })(),
      brier_model: brier,
      leak_risk_count: headline.reduce((a, r) => a + r.leak_risk, 0),
      regen_agreement: regen.length
        ? regen.reduce((a, r) => a + (r.regen_agrees ?? 0), 0) / regen.length : null,
    };
  });

  app.get<{ Querystring: { limit?: string } }>('/api/settlements', async (req) => {
    const limit = Math.min(Number(req.query.limit ?? 24) || 24, 200);
    const rows = all<Record<string, never>>(
      `SELECT s.event_id, s.result_total, s.pick_correct, s.settled_at,
              s.regen_agrees, f.start_time_utc, f.home_raw, f.away_raw,
              m.home_ft, m.away_ft, p.pick, p.tier, p.line, p.confidence,
              p.value_flag
       FROM settlements s
       JOIN fixtures f ON f.event_id = s.event_id
       LEFT JOIN matches m ON m.id = s.matched_match_id
       JOIN predictions p ON p.event_id = s.event_id AND p.predicted_at =
         (SELECT MAX(predicted_at) FROM predictions
          WHERE event_id = s.event_id AND predicted_at < f.start_time_utc)
       ORDER BY s.settled_at DESC, (p.pick IS NULL), p.confidence DESC`,
    );
    const seen = new Set<string>();
    return rows
      .filter((r: any) => seen.has(r.event_id) ? false : (seen.add(r.event_id), true))
      .slice(0, limit)
      .map((r: any) => ({
        ...r,
        home_club: CLUB(r.home_raw), away_club: CLUB(r.away_raw),
        outcome: r.pick == null ? 'no-pick'
          : r.pick_correct == null ? 'push'
          : r.pick_correct ? 'correct' : 'wrong',
      }));
  });

  app.get<{ Querystring: { days?: string } }>('/api/players', async (req) => {
    const days = Math.min(Number(req.query.days ?? 7) || 7, 60);
    const since = new Date(Date.now() - days * 86_400_000).toISOString().slice(0, 10);
    return all(
      `WITH sides AS (
         SELECT home_player p, home_ft gf, away_ft ga, home_ft+away_ft total
         FROM matches WHERE status = 3 AND home_ft IS NOT NULL AND date >= ?
         UNION ALL
         SELECT away_player, away_ft, home_ft, home_ft+away_ft
         FROM matches WHERE status = 3 AND home_ft IS NOT NULL AND date >= ?)
       SELECT p AS player, COUNT(*) AS matches,
              ROUND(AVG(gf), 2) AS gf_per_match,
              ROUND(AVG(ga), 2) AS ga_per_match,
              ROUND(AVG(total > 3.5), 3) AS over35_rate
       FROM sides GROUP BY p HAVING matches >= 5
       ORDER BY matches DESC, gf_per_match DESC`, since, since);
  });

  app.get<{ Querystring: { days?: string } }>('/api/analysis', async (req) => {
    const days = Math.min(Number(req.query.days ?? 30) || 30, 365);
    const since = new Date(Date.now() - days * 86_400_000).toISOString();
    const rows = all<{ event_id: string; result_total: number;
      pick_correct: number | null; regen_agrees: number | null;
      start_time_utc: string; home_raw: string; away_raw: string;
      pick: string | null; tier: string | null; line: number;
      confidence: number | null; p_over: number; p_under: number;
      value_flag: number }>(
      `SELECT s.event_id, s.result_total, s.pick_correct, s.regen_agrees,
              f.start_time_utc, f.home_raw, f.away_raw,
              p.pick, p.tier, p.line, p.confidence, p.p_over, p.p_under,
              p.value_flag
       FROM settlements s
       JOIN fixtures f ON f.event_id = s.event_id
       JOIN predictions p ON p.event_id = s.event_id AND p.predicted_at =
         (SELECT MAX(predicted_at) FROM predictions
          WHERE event_id = s.event_id AND predicted_at < f.start_time_utc)
       WHERE f.start_time_utc >= ?
       ORDER BY f.start_time_utc DESC, (p.pick IS NULL), p.confidence DESC`,
      since);
    const seen = new Set<string>();
    const headline = rows.filter((r) =>
      seen.has(r.event_id) ? false : (seen.add(r.event_id), true));

    const settlements = headline.map((r) => {
      // book's margin-free probs for the served line: last snapshot pre-kickoff
      const book = all<{ selection: string; implied_prob: number | null }>(
        `SELECT selection, implied_prob FROM odds_snapshots
         WHERE event_id = ? AND market = 'ou' AND line = ? AND fetched_at =
           (SELECT MAX(fetched_at) FROM odds_snapshots
            WHERE event_id = ? AND market = 'ou' AND line = ? AND fetched_at <= ?)`,
        r.event_id, r.line, r.event_id, r.line, r.start_time_utc);
      const bookOver = book.find((b) => b.selection === 'over')?.implied_prob ?? null;
      const bookUnder = book.find((b) => b.selection === 'under')?.implied_prob ?? null;
      return {
        event_id: r.event_id, kickoff: r.start_time_utc,
        home_club: CLUB(r.home_raw), away_club: CLUB(r.away_raw),
        home_player: (r.home_raw.match(/\(([^)]*)\)/) ?? [])[1] ?? null,
        away_player: (r.away_raw.match(/\(([^)]*)\)/) ?? [])[1] ?? null,
        line: r.line, pick: r.pick, tier: r.tier, confidence: r.confidence,
        value: !!r.value_flag,
        model_p_over: r.p_over, model_p_under: r.p_under,
        book_p_over: bookOver, book_p_under: bookUnder,
        result_total: r.result_total,
        outcome: r.pick == null ? 'no-pick'
          : r.pick_correct == null ? 'push'
          : r.pick_correct ? 'correct' : 'wrong',
      };
    });

    // daily hit-rate series (kickoff date, graded picks only)
    const byDay = new Map<string, { graded: number; hits: number }>();
    for (const r of headline) {
      if (r.pick_correct == null) continue;
      const d = r.start_time_utc.slice(0, 10);
      const e = byDay.get(d) ?? { graded: 0, hits: 0 };
      e.graded += 1;
      e.hits += r.pick_correct;
      byDay.set(d, e);
    }
    const daily = [...byDay.entries()].sort(([a], [b]) => a.localeCompare(b))
      .map(([date, e]) => ({ date, graded: e.graded, hits: e.hits,
                             hit_rate: e.hits / e.graded }));

    return { window_days: days, daily, settlements };
  });

  app.get('/api/health', async () => {
    const age = (iso: string | undefined | null) =>
      iso ? Math.round((Date.now() - Date.parse(iso)) / 60_000) : null;
    const lastMatch = one<{ t: string }>('SELECT MAX(scraped_at) t FROM matches');
    const lastOdds = one<{ t: string }>('SELECT MAX(fetched_at) t FROM odds_snapshots');
    const lastPred = one<{ t: string }>('SELECT MAX(predicted_at) t FROM predictions');
    const counts = one<{ m: number; f: number; p: number; s: number }>(
      `SELECT (SELECT COUNT(*) FROM matches) m, (SELECT COUNT(*) FROM fixtures) f,
              (SELECT COUNT(*) FROM predictions) p, (SELECT COUNT(*) FROM settlements) s`);
    const failures = Object.values(jobStatus).reduce((a, j) => a + j.failures, 0);
    const gapDays = resultsGapDays();
    return {
      ok: failures === 0 && gapDays <= 1,
      results_gap_days: gapDays, // > 1 = missing whole days (catch-up runs at boot)
      freshness_min: {
        results: age(lastMatch?.t), odds: age(lastOdds?.t),
        predictions: age(lastPred?.t),
      },
      counts,
      jobs: jobStatus,
    };
  });
}
