-- Two aliases missed by the 008 seed (docs/TEAM_RATINGS.md). 008 was built
-- from a loose (accent/case-folded) audit that false-counted these as already
-- matched, so they never entered its worklist; the EXACT-string resolver in
-- store.team_aliases then surfaced them as clubs-without-rating. Both are
-- present in sofifa FC25, differing from the canonical only by an accent
-- (''Atlético Madrid'') or case (''Olympique Lyonnais''). Same exact-join rule:
-- byte-exact both sides, generated from the tables. INSERT OR IGNORE.
INSERT OR IGNORE INTO team_rating_aliases (sofifa_name, club) VALUES
    ('Atlético Madrid', 'Atletico Madrid'),
    ('Olympique Lyonnais', 'Olympique lyonnais');
