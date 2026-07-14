-- Club identity resolution between the two sources (docs/CLUB_FEATURE.md).
--
-- The predictor reads clubs from betPawa fixture names ("Germany (Tifosi)");
-- the model trains on clubs from the GT Leagues results feed. They disagree on
-- 8 of 46 observed fixture club names. Unlike players — where a failed join
-- costs coverage loudly — a failed club join is SILENT: the club simply gets
-- catt = cdfn = 0 and the prediction quietly degrades to player-only. Hence
-- this table plus the unresolved-name counter in store/aliases.py.
CREATE TABLE club_aliases (
    book_name    TEXT PRIMARY KEY,             -- name as it appears in fixtures
    club         TEXT NOT NULL                 -- canonical matches.*_club spelling
);

-- Seeded from the 2026-07-10 audit of every distinct fixture club name.
-- INSERT OR IGNORE so re-seeding never fights a hand-added row.
INSERT OR IGNORE INTO club_aliases (book_name, club) VALUES
    ('Bosnia And Herzegovina', 'Bosnia Herzegovina'),
    ('Congo Dr',               'DR Congo'),
    ('Czechia',                'Czech Republic'),
    ('Ivory Coast',            'Cote d'' Ivoire'),
    ('Korea Republic',         'South Korea'),
    ('Turkiye',                'Turkey');

-- Deliberately NOT seeded: betPawa lists both 'USA' and 'United States', and
-- neither has a finished match in the results feed yet — there is no canonical
-- spelling to point at. They are a genuine cold start, not an alias. Both fall
-- back to league-average club effect until they play; add the mapping then.
