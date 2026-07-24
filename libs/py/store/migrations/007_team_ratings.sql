-- External FC25 team ratings scraped from sofifa.com/teams (docs/TEAM_RATINGS.md).
--
-- Reference data, NOT a live feed. Ratings change only when sofifa publishes a
-- new edition (the `r=` URL param, stored here as `edition`), so this table is
-- refreshed BY HAND via `python -m ratings_ingest.cli refresh`, never by the
-- predictor timer. sofifa is behind Cloudflare; the refresh carries a pasted
-- cookie and fails loudly when it goes stale.
--
-- Why store it at all: the value here is EXTERNAL to the goal data. The joint
-- GLM already contains a club's goal-derived strength (docs/CLUB_FEATURE.md
-- §Re-evaluation found club strength in isolation is redundant with catt-cdfn),
-- so these ratings are only non-redundant as a COLD-START prior for clubs the
-- model has no finished match for. This migration is storage only — nothing
-- reads team_ratings yet.
CREATE TABLE team_ratings (
    sofifa_id       INTEGER NOT NULL,           -- stable across editions (/team/<id>/)
    edition         TEXT NOT NULL,              -- roster version pin, e.g. '260045'
    sofifa_name     TEXT NOT NULL,              -- clean UTF-8 (joins need normalization)
    nationality     TEXT,                       -- flag title; == country for national teams
    league          TEXT,                       -- league name (national teams sit in a distinct one)
    league_id       INTEGER,                    -- from /league/<id>
    is_national     INTEGER NOT NULL DEFAULT 0, -- national team vs club, decided at parse time
    overall         INTEGER,
    attack          INTEGER,
    midfield        INTEGER,
    defence         INTEGER,
    domestic_prestige      INTEGER,
    international_prestige  INTEGER,
    num_players     INTEGER,
    starting_age    REAL,                        -- starting XI average age
    transfer_budget REAL,                        -- euros, parsed from e.g. '€19.9M'
    club_worth      REAL,                        -- euros, parsed from e.g. '€308.7M'
    raw_hash        TEXT NOT NULL,               -- sha1 of source cells, for idempotent upsert
    scraped_at      TEXT NOT NULL,
    PRIMARY KEY (sofifa_id, edition)
);
CREATE INDEX idx_team_ratings_name ON team_ratings(sofifa_name);

-- sofifa spelling -> canonical matches.*_club spelling. Mirrors club_aliases
-- (the betPawa->results bridge); this is the sofifa->results bridge. Only the
-- MISMATCHES need a row — most sofifa names normalize-match the canonical club
-- directly. Seeded in a later step from a measured coverage audit, NOT guessed:
-- the exact sofifa spellings (e.g. 'Paris Saint-Germain' -> 'PSG', 'Milan' ->
-- 'AC Milan') are only known once scraped. A missing row is SILENT — same
-- failure mode as club_aliases: the rating fails to join and the club keeps its
-- GLM-only strength, with no error anywhere. The only defense is counting
-- unresolved names, never rewriting silently.
CREATE TABLE team_rating_aliases (
    sofifa_name  TEXT PRIMARY KEY,
    club         TEXT NOT NULL                   -- canonical matches.*_club spelling
);
