-- Paper-trading ledger (docs/WAGERS.md): user-placed hypothetical slips over
-- the book-priced slate. The ONE writable corner of an otherwise read-only
-- store — these are user state, not model output, so they live in their own
-- tables and never touch predictions/settlements.
--
-- Grading is DERIVED at read time by joining settlements/settlements_x12, never
-- stored here: a wager is a frozen snapshot of stake + leg odds at placement,
-- and its result follows whatever the settlement loops later record. Only
-- book-priced fixtures are bettable — schedule-only gtl: events never settle,
-- so the API rejects them before a row is ever written.

CREATE TABLE wagers (
    id         INTEGER PRIMARY KEY,
    placed_at  TEXT NOT NULL,
    stake      REAL NOT NULL,           -- units
    total_odds REAL NOT NULL            -- product of leg odds at placement
);
CREATE TABLE wager_legs (
    id        INTEGER PRIMARY KEY,
    wager_id  INTEGER NOT NULL REFERENCES wagers(id),
    event_id  TEXT NOT NULL,
    market    TEXT NOT NULL,            -- 'ou' | '1x2'
    line      REAL,                     -- ou only
    selection TEXT NOT NULL,            -- over/under | home/draw/away
    odds      REAL NOT NULL             -- decimal, snapshotted at placement
);
CREATE INDEX idx_wager_legs_wager ON wager_legs(wager_id);
