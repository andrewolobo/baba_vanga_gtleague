-- Plan §3.2. The matches UNIQUE constraint IS the dedup lesson (parent §2.2.9).
CREATE TABLE matches (
    id              INTEGER PRIMARY KEY,
    source_match_id TEXT NOT NULL UNIQUE,
    date            TEXT NOT NULL,              -- UTC date YYYY-MM-DD
    kickoff_ts      TEXT NOT NULL,              -- ISO-8601 UTC
    competition     TEXT NOT NULL,
    home_player     TEXT NOT NULL,
    away_player     TEXT NOT NULL,
    home_club       TEXT NOT NULL,
    away_club       TEXT NOT NULL,
    status          INTEGER NOT NULL,           -- 0 scheduled / 3 finished / 4 cancelled
    home_ft         INTEGER,                    -- null unless finished
    away_ft         INTEGER,
    raw_hash        TEXT NOT NULL,
    scraped_at      TEXT NOT NULL,
    UNIQUE(date, kickoff_ts, home_player, away_player, home_ft, away_ft)
);
CREATE INDEX idx_matches_kickoff ON matches(kickoff_ts);
CREATE INDEX idx_matches_player ON matches(home_player, away_player);

CREATE TABLE fixtures (
    event_id        TEXT PRIMARY KEY,
    start_time_utc  TEXT NOT NULL,
    competition     TEXT NOT NULL,
    home_raw        TEXT NOT NULL,
    away_raw        TEXT NOT NULL,
    home_player     TEXT,
    away_player     TEXT,
    coverage        TEXT NOT NULL DEFAULT 'none',   -- full|partial|none (alias join)
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);
CREATE INDEX idx_fixtures_start ON fixtures(start_time_utc);

-- Append-only: full odds history = free CLV analysis later.
CREATE TABLE odds_snapshots (
    id           INTEGER PRIMARY KEY,
    event_id     TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    market       TEXT NOT NULL,                 -- '1x2' | 'ou'
    line         REAL,                          -- ou only
    selection    TEXT NOT NULL,                 -- home/draw/away | over/under
    odds         REAL NOT NULL,
    implied_prob REAL
);
CREATE INDEX idx_odds_event ON odds_snapshots(event_id, fetched_at);

-- Append-only prediction log (replaces the parent's overwritten JSON).
CREATE TABLE predictions (
    id              INTEGER PRIMARY KEY,
    event_id        TEXT NOT NULL,
    predicted_at    TEXT NOT NULL,
    totals_source   TEXT NOT NULL,              -- 'poisson' | 'blend'
    line            REAL NOT NULL,
    p_over          REAL NOT NULL,
    p_push          REAL NOT NULL,
    p_under         REAL NOT NULL,
    lambda_home     REAL,
    lambda_away     REAL,
    pick            TEXT,
    confidence      REAL,
    tier            TEXT,
    value_flag      INTEGER NOT NULL DEFAULT 0,
    model_version   TEXT NOT NULL,
    as_of_cutoff_ts TEXT NOT NULL
);
CREATE INDEX idx_predictions_event ON predictions(event_id, predicted_at);

CREATE TABLE settlements (
    event_id         TEXT PRIMARY KEY,
    matched_match_id INTEGER,
    offset_min_used  REAL,
    result_total     INTEGER,
    pick_correct     INTEGER,
    leak_risk        INTEGER NOT NULL DEFAULT 0,
    regen_pick       TEXT,
    regen_agrees     INTEGER,
    settled_at       TEXT NOT NULL
);

CREATE TABLE model_runs (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,
    fitted_at    TEXT NOT NULL,
    train_rows   INTEGER,
    params_json  TEXT,
    metrics_json TEXT
);

CREATE TABLE player_aliases (
    book_name    TEXT PRIMARY KEY,              -- name as extracted from the feed
    player       TEXT NOT NULL                  -- nickname as in matches.*_player
);
