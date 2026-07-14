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

interface X12Row {
  event_id: string; predicted_at: string;
  p_home: number; p_draw: number; p_away: number;
  lambda_home: number; lambda_away: number;
  pick: string | null; confidence: number | null;
  value_flag: number; model_version: string;
}

type X12Odds = Record<string, { odds: number; implied: number | null }>;

// Latest served 1x2 batch, shaped for a slate card. One row per fixture per
// cycle — no line fan-out. Book fields stay null for unpriced (gtl:) rows,
// mirroring the totals lines' unpriced branch; null when the head is off or
// hasn't covered the fixture (docs/X12_UI.md Phase 1).
function x12View(eventId: string, book: X12Odds | null) {
  const r = one<X12Row>(
    `SELECT * FROM predictions_x12 WHERE event_id = ? AND predicted_at =
       (SELECT MAX(predicted_at) FROM predictions_x12 WHERE event_id = ?)`,
    eventId, eventId);
  if (!r) return null;
  const probs = { home: r.p_home, draw: r.p_draw, away: r.p_away };
  // edge is for the model-favored outcome, pick or not (same rule as totals)
  const side = (['home', 'draw', 'away'] as const)
    .reduce((a, b) => (probs[b] > probs[a] ? b : a));
  const bookSide = book?.[side]?.implied ?? null;
  return {
    p_home: r.p_home, p_draw: r.p_draw, p_away: r.p_away,
    pick: r.pick, confidence: r.confidence, value: !!r.value_flag,
    odds: { home: book?.home?.odds ?? null, draw: book?.draw?.odds ?? null,
            away: book?.away?.odds ?? null },
    book: { home: book?.home?.implied ?? null, draw: book?.draw?.implied ?? null,
            away: book?.away?.implied ?? null },
    edge: bookSide != null
      ? Math.round((probs[side] - bookSide) * 1000) / 1000 : null,
  };
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
        x12: x12View(f.event_id, odds.x12),
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
          x12: x12View(`gtl:${m.source_match_id}`, null),
        };
      })
      .filter((x) => x != null);

    return [...priced, ...unpriced]
      .sort((a, b) => a.kickoff.localeCompare(b.kickoff));
  });

  // Two prediction populations, never pooled (docs/POPULATION_SPLIT.md):
  // book-priced fixtures vs schedule-only gtl: rows. The priced query's
  // fixtures join IS its population filter; the schedule query goes through
  // settlements.matched_match_id.
  interface MetricRow { event_id: string; pick_correct: number | null;
    leak_risk: number; regen_agrees: number | null; tier: string | null;
    pick: string | null; value_flag: number; p_over: number; line: number;
    result_total: number }

  const METRIC_PRICED = `
    SELECT s.event_id, s.pick_correct, s.leak_risk, s.regen_agrees,
           s.result_total, p.tier, p.pick, p.value_flag, p.p_over, p.line
    FROM settlements s
    JOIN fixtures f ON f.event_id = s.event_id
    JOIN predictions p ON p.event_id = s.event_id AND p.predicted_at =
      (SELECT MAX(predicted_at) FROM predictions
       WHERE event_id = s.event_id AND predicted_at < f.start_time_utc)
    WHERE s.settled_at >= ?
    ORDER BY s.event_id, (p.pick IS NULL), p.confidence DESC`;

  const METRIC_SCHEDULE = `
    SELECT s.event_id, s.pick_correct, s.leak_risk, s.regen_agrees,
           s.result_total, p.tier, p.pick, p.value_flag, p.p_over, p.line
    FROM settlements s
    JOIN matches m ON m.id = s.matched_match_id
    JOIN predictions p ON p.event_id = s.event_id AND p.predicted_at =
      (SELECT MAX(predicted_at) FROM predictions
       WHERE event_id = s.event_id AND predicted_at < m.kickoff_ts)
    WHERE s.settled_at >= ? AND s.event_id LIKE 'gtl:%'
    ORDER BY s.event_id, (p.pick IS NULL), p.confidence DESC`;

  // headline row per settled event = highest-confidence picked line of the
  // last batch served before kickoff (same rule as settlement grading)
  function summarize(rows: MetricRow[]) {
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
  }

  // 1x2 mirrors the totals population split (same joins, x12 tables). One row
  // per fixture per cycle, so no headline-line dedup. No tiers, deliberately:
  // none exist until bands are quantiled on served confidence
  // (docs/X12_SERVING.md); confidence is served raw instead.
  interface X12MetricRow { event_id: string; pick_correct: number | null;
    regen_agrees: number | null; pick: string | null; value_flag: number }

  const X12_METRIC_PRICED = `
    SELECT s.event_id, s.pick_correct, s.regen_agrees, p.pick, p.value_flag
    FROM settlements_x12 s
    JOIN fixtures f ON f.event_id = s.event_id
    JOIN predictions_x12 p ON p.event_id = s.event_id AND p.predicted_at =
      (SELECT MAX(predicted_at) FROM predictions_x12
       WHERE event_id = s.event_id AND predicted_at < f.start_time_utc)
    WHERE s.settled_at >= ?`;

  const X12_METRIC_SCHEDULE = `
    SELECT s.event_id, s.pick_correct, s.regen_agrees, p.pick, p.value_flag
    FROM settlements_x12 s
    JOIN matches m ON m.id = s.matched_match_id
    JOIN predictions_x12 p ON p.event_id = s.event_id AND p.predicted_at =
      (SELECT MAX(predicted_at) FROM predictions_x12
       WHERE event_id = s.event_id AND predicted_at < m.kickoff_ts)
    WHERE s.settled_at >= ? AND s.event_id LIKE 'gtl:%'`;

  function summarizeX12(rows: X12MetricRow[]) {
    const graded = rows.filter((r) => r.pick_correct != null);
    const hit = (xs: X12MetricRow[]) => xs.length
      ? xs.reduce((a, r) => a + (r.pick_correct ?? 0), 0) / xs.length : null;
    const regen = rows.filter((r) => r.regen_agrees != null);
    const value = graded.filter((r) => r.value_flag === 1);
    return {
      settled: rows.length,
      graded: graded.length,
      hit_rate: hit(graded),
      value: { n: value.length, hit_rate: hit(value) },
      regen_agreement: regen.length
        ? regen.reduce((a, r) => a + (r.regen_agrees ?? 0), 0) / regen.length
        : null,
    };
  }

  app.get<{ Querystring: { days?: string } }>('/api/metrics', async (req) => {
    const days = Math.min(Number(req.query.days ?? 7) || 7, 90);
    const since = new Date(Date.now() - days * 86_400_000).toISOString();
    return {
      window_days: days,
      priced: summarize(all<MetricRow>(METRIC_PRICED, since)),
      schedule: summarize(all<MetricRow>(METRIC_SCHEDULE, since)),
      x12: {
        priced: summarizeX12(all<X12MetricRow>(X12_METRIC_PRICED, since)),
        schedule: summarizeX12(all<X12MetricRow>(X12_METRIC_SCHEDULE, since)),
      },
    };
  });

  app.get<{ Querystring: { limit?: string } }>('/api/settlements', async (req) => {
    const limit = Math.min(Number(req.query.limit ?? 24) || 24, 200);
    const priced = all<Record<string, never>>(
      `SELECT s.event_id, s.result_total, s.pick_correct, s.settled_at,
              s.regen_agrees, f.start_time_utc, f.home_raw, f.away_raw,
              m.home_ft, m.away_ft, p.pick, p.tier, p.line, p.confidence,
              p.value_flag, 'priced' AS population,
              sx.result AS x12_result, sx.pick_correct AS x12_correct,
              px.pick AS x12_pick, px.confidence AS x12_confidence
       FROM settlements s
       JOIN fixtures f ON f.event_id = s.event_id
       LEFT JOIN matches m ON m.id = s.matched_match_id
       JOIN predictions p ON p.event_id = s.event_id AND p.predicted_at =
         (SELECT MAX(predicted_at) FROM predictions
          WHERE event_id = s.event_id AND predicted_at < f.start_time_utc)
       LEFT JOIN settlements_x12 sx ON sx.event_id = s.event_id
       LEFT JOIN predictions_x12 px ON px.event_id = s.event_id
         AND px.predicted_at =
         (SELECT MAX(predicted_at) FROM predictions_x12
          WHERE event_id = s.event_id AND predicted_at < f.start_time_utc)
       ORDER BY s.settled_at DESC, (p.pick IS NULL), p.confidence DESC
       LIMIT 2000`,
    );
    const schedule = all<Record<string, never>>(
      `SELECT s.event_id, s.result_total, s.pick_correct, s.settled_at,
              s.regen_agrees, m.kickoff_ts AS start_time_utc,
              m.home_club || ' (' || m.home_player || ')' AS home_raw,
              m.away_club || ' (' || m.away_player || ')' AS away_raw,
              m.home_ft, m.away_ft, p.pick, p.tier, p.line, p.confidence,
              p.value_flag, 'schedule' AS population,
              sx.result AS x12_result, sx.pick_correct AS x12_correct,
              px.pick AS x12_pick, px.confidence AS x12_confidence
       FROM settlements s
       JOIN matches m ON m.id = s.matched_match_id
       JOIN predictions p ON p.event_id = s.event_id AND p.predicted_at =
         (SELECT MAX(predicted_at) FROM predictions
          WHERE event_id = s.event_id AND predicted_at < m.kickoff_ts)
       LEFT JOIN settlements_x12 sx ON sx.event_id = s.event_id
       LEFT JOIN predictions_x12 px ON px.event_id = s.event_id
         AND px.predicted_at =
         (SELECT MAX(predicted_at) FROM predictions_x12
          WHERE event_id = s.event_id AND predicted_at < m.kickoff_ts)
       WHERE s.event_id LIKE 'gtl:%'
       ORDER BY s.settled_at DESC, (p.pick IS NULL), p.confidence DESC
       LIMIT 2000`,
    );
    // event ids never collide across populations; dedup to the headline row,
    // then interleave by settled time
    const seen = new Set<string>();
    return [...priced, ...schedule]
      .filter((r: any) => seen.has(r.event_id) ? false : (seen.add(r.event_id), true))
      .sort((a: any, b: any) => b.settled_at.localeCompare(a.settled_at))
      .slice(0, limit)
      .map((r: any) => ({
        ...r,
        home_club: CLUB(r.home_raw), away_club: CLUB(r.away_raw),
        outcome: r.pick == null ? 'no-pick'
          : r.pick_correct == null ? 'push'
          : r.pick_correct ? 'correct' : 'wrong',
        // null until the event is settled for x12 (head enabled later than
        // this event, or grading pending) — no push state in a 3-way market
        x12_outcome: r.x12_result == null ? null
          : r.x12_pick == null ? 'no-pick'
          : r.x12_correct ? 'correct' : 'wrong',
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
    type AnalysisRow = { event_id: string; result_total: number;
      pick_correct: number | null; regen_agrees: number | null;
      start_time_utc: string; home_raw: string; away_raw: string;
      pick: string | null; tier: string | null; line: number;
      confidence: number | null; p_over: number; p_under: number;
      lambda_home: number | null; value_flag: number;
      home_ft: number | null; away_ft: number | null; population: string };
    const rows = all<AnalysisRow>(
      `SELECT s.event_id, s.result_total, s.pick_correct, s.regen_agrees,
              f.start_time_utc, f.home_raw, f.away_raw,
              p.pick, p.tier, p.line, p.confidence, p.p_over, p.p_under,
              p.lambda_home, p.value_flag, m.home_ft, m.away_ft,
              'priced' AS population
       FROM settlements s
       JOIN fixtures f ON f.event_id = s.event_id
       LEFT JOIN matches m ON m.id = s.matched_match_id
       JOIN predictions p ON p.event_id = s.event_id AND p.predicted_at =
         (SELECT MAX(predicted_at) FROM predictions
          WHERE event_id = s.event_id AND predicted_at < f.start_time_utc)
       WHERE f.start_time_utc >= ?
       ORDER BY f.start_time_utc DESC, (p.pick IS NULL), p.confidence DESC`,
      since);
    // schedule-only population: model picks without a book price, graded the
    // same way (docs/POPULATION_SPLIT.md Phase 3) — book_* stay null
    const schedRows = all<AnalysisRow>(
      `SELECT s.event_id, s.result_total, s.pick_correct, s.regen_agrees,
              m.kickoff_ts AS start_time_utc,
              m.home_club || ' (' || m.home_player || ')' AS home_raw,
              m.away_club || ' (' || m.away_player || ')' AS away_raw,
              p.pick, p.tier, p.line, p.confidence, p.p_over, p.p_under,
              p.lambda_home, p.value_flag, m.home_ft, m.away_ft,
              'schedule' AS population
       FROM settlements s
       JOIN matches m ON m.id = s.matched_match_id
       JOIN predictions p ON p.event_id = s.event_id AND p.predicted_at =
         (SELECT MAX(predicted_at) FROM predictions
          WHERE event_id = s.event_id AND predicted_at < m.kickoff_ts)
       WHERE s.event_id LIKE 'gtl:%' AND m.kickoff_ts >= ?
       ORDER BY m.kickoff_ts DESC, (p.pick IS NULL), p.confidence DESC`,
      since);
    const seen = new Set<string>();
    const headline = [...rows, ...schedRows]
      .filter((r) => seen.has(r.event_id) ? false : (seen.add(r.event_id), true))
      .sort((a, b) => b.start_time_utc.localeCompare(a.start_time_utc));

    const settlements = headline.map((r) => {
      // book's margin-free probs for the served line: last snapshot
      // pre-kickoff. Schedule events have no odds — skip the lookup.
      const book = r.population === 'priced'
        ? all<{ selection: string; implied_prob: number | null }>(
            `SELECT selection, implied_prob FROM odds_snapshots
             WHERE event_id = ? AND market = 'ou' AND line = ? AND fetched_at =
               (SELECT MAX(fetched_at) FROM odds_snapshots
                WHERE event_id = ? AND market = 'ou' AND line = ? AND fetched_at <= ?)`,
            r.event_id, r.line, r.event_id, r.line, r.start_time_utc)
        : [];
      const bookOver = book.find((b) => b.selection === 'over')?.implied_prob ?? null;
      const bookUnder = book.find((b) => b.selection === 'under')?.implied_prob ?? null;
      // Which way the model leaned, whether or not it cleared the pick gate.
      // Uncovered rows served the book's own price back, so they have no lean.
      // model_side_correct is a SHADOW grade: it never feeds hit_rate or any
      // tier stat (those read settlements.pick_correct, still null here) — it
      // only lets you see what the gate suppressed.
      const covered = r.lambda_home != null;
      const side = !covered ? null : r.p_over > r.p_under ? 'over' : 'under';
      const push = r.result_total === r.line;
      const sideCorrect = side == null || push ? null
        : Number(side === 'over' ? r.result_total > r.line
                                 : r.result_total < r.line);
      return {
        event_id: r.event_id, kickoff: r.start_time_utc,
        population: r.population,
        home_club: CLUB(r.home_raw), away_club: CLUB(r.away_raw),
        home_player: (r.home_raw.match(/\(([^)]*)\)/) ?? [])[1] ?? null,
        away_player: (r.away_raw.match(/\(([^)]*)\)/) ?? [])[1] ?? null,
        line: r.line, pick: r.pick, tier: r.tier, confidence: r.confidence,
        value: !!r.value_flag,
        model_p_over: r.p_over, model_p_under: r.p_under,
        model_side: side, model_side_correct: sideCorrect,
        book_p_over: bookOver, book_p_under: bookUnder,
        home_ft: r.home_ft, away_ft: r.away_ft,
        result_total: r.result_total,
        outcome: r.pick == null ? 'no-pick'
          : r.pick_correct == null ? 'push'
          : r.pick_correct ? 'correct' : 'wrong',
      };
    });

    // daily hit-rate series (kickoff date, graded picks only) — priced
    // population only: the Analysis tab is the model-vs-book view, and the
    // populations must never be pooled into one series
    const byDay = new Map<string, { graded: number; hits: number }>();
    for (const r of headline) {
      if (r.population !== 'priced' || r.pick_correct == null) continue;
      const d = r.start_time_utc.slice(0, 10);
      const e = byDay.get(d) ?? { graded: 0, hits: 0 };
      e.graded += 1;
      e.hits += r.pick_correct;
      byDay.set(d, e);
    }
    const daily = [...byDay.entries()].sort(([a], [b]) => a.localeCompare(b))
      .map(([date, e]) => ({ date, graded: e.graded, hits: e.hits,
                             hit_rate: e.hits / e.graded }));

    // 1x2 mirror of the settled-event frame (docs/X12_UI.md Phase 3): one row
    // per x12-settled event, graded on the model's top outcome whether or not
    // it cleared the 0.50 gate. `side_correct` is the x12 SHADOW grade — like
    // model_side_correct above it never feeds the Ledger scorecard, which
    // reads settlements_x12.pick_correct. No pushes: 3-way markets always
    // grade. Populations carry the same never-pool rule.
    type X12AnalysisRow = { event_id: string; result: string;
      pick_correct: number | null; regen_agrees: number | null;
      start_time_utc: string; home_raw: string; away_raw: string;
      p_home: number; p_draw: number; p_away: number;
      pick: string | null; confidence: number | null; value_flag: number;
      home_ft: number | null; away_ft: number | null; population: string };
    const x12Priced = all<X12AnalysisRow>(
      `SELECT s.event_id, s.result, s.pick_correct, s.regen_agrees,
              f.start_time_utc, f.home_raw, f.away_raw,
              p.p_home, p.p_draw, p.p_away, p.pick, p.confidence, p.value_flag,
              m.home_ft, m.away_ft, 'priced' AS population
       FROM settlements_x12 s
       JOIN fixtures f ON f.event_id = s.event_id
       LEFT JOIN matches m ON m.id = s.matched_match_id
       JOIN predictions_x12 p ON p.event_id = s.event_id AND p.predicted_at =
         (SELECT MAX(predicted_at) FROM predictions_x12
          WHERE event_id = s.event_id AND predicted_at < f.start_time_utc)
       WHERE f.start_time_utc >= ?`, since);
    const x12Sched = all<X12AnalysisRow>(
      `SELECT s.event_id, s.result, s.pick_correct, s.regen_agrees,
              m.kickoff_ts AS start_time_utc,
              m.home_club || ' (' || m.home_player || ')' AS home_raw,
              m.away_club || ' (' || m.away_player || ')' AS away_raw,
              p.p_home, p.p_draw, p.p_away, p.pick, p.confidence, p.value_flag,
              m.home_ft, m.away_ft, 'schedule' AS population
       FROM settlements_x12 s
       JOIN matches m ON m.id = s.matched_match_id
       JOIN predictions_x12 p ON p.event_id = s.event_id AND p.predicted_at =
         (SELECT MAX(predicted_at) FROM predictions_x12
          WHERE event_id = s.event_id AND predicted_at < m.kickoff_ts)
       WHERE s.event_id LIKE 'gtl:%' AND m.kickoff_ts >= ?`, since);
    const x12 = [...x12Priced, ...x12Sched]
      .sort((a, b) => b.start_time_utc.localeCompare(a.start_time_utc))
      .map((r) => {
        const probs = { home: r.p_home, draw: r.p_draw, away: r.p_away };
        const side = (['home', 'draw', 'away'] as const)
          .reduce((a, b) => (probs[b] > probs[a] ? b : a));
        const book = r.population === 'priced'
          ? all<{ selection: string; implied_prob: number | null }>(
              `SELECT selection, implied_prob FROM odds_snapshots
               WHERE event_id = ? AND market = '1x2' AND fetched_at =
                 (SELECT MAX(fetched_at) FROM odds_snapshots
                  WHERE event_id = ? AND market = '1x2' AND fetched_at <= ?)`,
              r.event_id, r.event_id, r.start_time_utc)
          : [];
        const bp = (k: string) =>
          book.find((b) => b.selection === k)?.implied_prob ?? null;
        return {
          event_id: r.event_id, kickoff: r.start_time_utc,
          population: r.population,
          home_club: CLUB(r.home_raw), away_club: CLUB(r.away_raw),
          home_player: (r.home_raw.match(/\(([^)]*)\)/) ?? [])[1] ?? null,
          away_player: (r.away_raw.match(/\(([^)]*)\)/) ?? [])[1] ?? null,
          pick: r.pick, confidence: r.confidence, value: !!r.value_flag,
          p_home: r.p_home, p_draw: r.p_draw, p_away: r.p_away,
          side, side_correct: Number(side === r.result),
          book_p_home: bp('home'), book_p_draw: bp('draw'),
          book_p_away: bp('away'),
          result: r.result, home_ft: r.home_ft, away_ft: r.away_ft,
          regen_agrees: r.regen_agrees,
          outcome: r.pick == null ? 'no-pick'
            : r.pick_correct ? 'correct' : 'wrong',
        };
      });

    return { window_days: days, daily, settlements, x12 };
  });

  // ── λ-slope trajectory ────────────────────────────────────────────────
  //
  // Daily series of vs-book's "lambda dynamic range" slope: OLS of realized
  // total on served λ-total over a rolling 7-day window ending each day.
  // Deliberately the cheap read — analytic standard error, no bootstrap,
  // pushes kept, no book-price requirement — so it tracks but won't exactly
  // equal `settlement.settle vs-book`; the CLI stays the careful number.
  // Populations are computed separately, never pooled.
  interface SlopeRow { day: string; lam_total: number; result_total: number }

  // GROUP BY event collapses the totals line fan-out — λ is identical across
  // lines within a served batch, so any row of the batch carries it.
  const SLOPE_PRICED = `
    SELECT date(s.settled_at) AS day,
           p.lambda_home + p.lambda_away AS lam_total, s.result_total
    FROM settlements s
    JOIN fixtures f ON f.event_id = s.event_id
    JOIN predictions p ON p.event_id = s.event_id AND p.predicted_at =
      (SELECT MAX(predicted_at) FROM predictions
       WHERE event_id = s.event_id AND predicted_at < f.start_time_utc)
    WHERE s.settled_at >= ? AND s.leak_risk = 0
      AND s.result_total IS NOT NULL AND p.lambda_home IS NOT NULL
    GROUP BY s.event_id`;

  const SLOPE_SCHEDULE = `
    SELECT date(s.settled_at) AS day,
           p.lambda_home + p.lambda_away AS lam_total, s.result_total
    FROM settlements s
    JOIN matches m ON m.id = s.matched_match_id
    JOIN predictions p ON p.event_id = s.event_id AND p.predicted_at =
      (SELECT MAX(predicted_at) FROM predictions
       WHERE event_id = s.event_id AND predicted_at < m.kickoff_ts)
    WHERE s.settled_at >= ? AND s.event_id LIKE 'gtl:%' AND s.leak_risk = 0
      AND s.result_total IS NOT NULL AND p.lambda_home IS NOT NULL
    GROUP BY s.event_id`;

  const SLOPE_WINDOW = 7;   // matches the CLI habit: vs-book --days 7
  const SLOPE_MIN_N = 25;   // below this the daily point is pure noise

  function olsSlope(rows: SlopeRow[]) {
    const n = rows.length;
    if (n < SLOPE_MIN_N) return null;
    let mx = 0, my = 0;
    for (const r of rows) { mx += r.lam_total; my += r.result_total; }
    mx /= n; my /= n;
    let sxx = 0, sxy = 0;
    for (const r of rows) {
      sxx += (r.lam_total - mx) ** 2;
      sxy += (r.lam_total - mx) * (r.result_total - my);
    }
    if (sxx < 1e-9) return null;
    const slope = sxy / sxx;
    let sse = 0;
    for (const r of rows) {
      const e = r.result_total - my - slope * (r.lam_total - mx);
      sse += e * e;
    }
    const se = Math.sqrt(sse / (n - 2) / sxx);
    return { slope: Math.round(slope * 1000) / 1000,
             se: Math.round(se * 1000) / 1000, n };
  }

  app.get<{ Querystring: { days?: string } }>('/api/trajectory', async (req) => {
    const days = Math.min(Number(req.query.days ?? 30) || 30, 90);
    const since = new Date(Date.now()
      - (days + SLOPE_WINDOW - 1) * 86_400_000).toISOString();
    const byDay = (rows: SlopeRow[]) => {
      const m = new Map<string, SlopeRow[]>();
      for (const r of rows) {
        const xs = m.get(r.day);
        if (xs) xs.push(r); else m.set(r.day, [r]);
      }
      return m;
    };
    const pools = {
      priced: byDay(all<SlopeRow>(SLOPE_PRICED, since)),
      schedule: byDay(all<SlopeRow>(SLOPE_SCHEDULE, since)),
    };
    const dayISO = (back: number) =>
      new Date(Date.now() - back * 86_400_000).toISOString().slice(0, 10);
    const series = [];
    for (let d = days - 1; d >= 0; d--) {
      const dates = Array.from({ length: SLOPE_WINDOW }, (_, i) => dayISO(d + i));
      const windowOf = (m: Map<string, SlopeRow[]>) =>
        dates.flatMap((dt) => m.get(dt) ?? []);
      series.push({
        date: dayISO(d),
        priced: olsSlope(windowOf(pools.priced)),
        schedule: olsSlope(windowOf(pools.schedule)),
      });
    }

    // graded 1x2 pick counts drive the data-gated milestones on the
    // trajectory tab: cumulative totals plus recent pace, per population
    const graded = (op: string) => one<{ total: number; d3: number; d7: number }>(
      `SELECT COUNT(*) AS total,
              COALESCE(SUM(settled_at >= ?), 0) AS d3,
              COALESCE(SUM(settled_at >= ?), 0) AS d7
       FROM settlements_x12
       WHERE pick_correct IS NOT NULL AND event_id ${op} 'gtl:%'`,
      new Date(Date.now() - 3 * 86_400_000).toISOString(),
      new Date(Date.now() - 7 * 86_400_000).toISOString());
    return {
      window_days: days, slope_window: SLOPE_WINDOW, min_n: SLOPE_MIN_N,
      series,
      x12_graded: { schedule: graded('LIKE'), priced: graded('NOT LIKE') },
    };
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
