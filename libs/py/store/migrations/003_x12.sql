-- 1x2 serving head (docs/CLUB_FEATURE.md gate PASSED 2026-07-11).
--
-- Separate tables, not a `market` column on predictions/settlements: the
-- totals columns (line, p_over/p_push/p_under, result_total) do not mean
-- anything for a 3-outcome market, and overloading them is exactly the
-- semantics drift the confidence-column incident documented. Separate tables
-- also make the transition safe — events settled for totals before this ships
-- still get x12 grading, because each loop has its own NOT EXISTS.

CREATE TABLE predictions_x12 (
    id              INTEGER PRIMARY KEY,
    event_id        TEXT NOT NULL,
    predicted_at    TEXT NOT NULL,
    p_home          REAL NOT NULL,
    p_draw          REAL NOT NULL,
    p_away          REAL NOT NULL,
    lambda_home     REAL NOT NULL,               -- always model-covered: no book fallback here
    lambda_away     REAL NOT NULL,
    pick            TEXT,                        -- home/draw/away; NULL below the x12 gate
    confidence      REAL,                        -- max(p_home, p_draw, p_away) when picked
    value_flag      INTEGER NOT NULL DEFAULT 0,
    model_version   TEXT NOT NULL,
    as_of_cutoff_ts TEXT NOT NULL
);
CREATE INDEX idx_predictions_x12_event ON predictions_x12(event_id, predicted_at);

CREATE TABLE settlements_x12 (
    event_id         TEXT PRIMARY KEY,
    matched_match_id INTEGER,
    result           TEXT NOT NULL,              -- home/draw/away, from FT score
    pick_correct     INTEGER,                    -- NULL when the served row had no pick
    regen_pick       TEXT,
    regen_agrees     INTEGER,
    settled_at       TEXT NOT NULL
);
