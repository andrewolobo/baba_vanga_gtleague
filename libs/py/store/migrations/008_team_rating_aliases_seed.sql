-- Seed team_rating_aliases: sofifa /teams spelling -> canonical matches.*_club
-- spelling (docs/TEAM_RATINGS.md). The sofifa->results bridge, mirroring
-- club_aliases (the betPawa->results one). Generated 2026-07-22 from a verified
-- audit of the 598 scraped FC25 ratings against every canonical club with a
-- finished match; only the ~37 names that are NOT byte-identical need a row
-- (exact matches like ''Manchester City'' resolve with no alias).
--
-- Both sides are clean UTF-8 with accents intact (sofifa ''Fenerbahçe SK'',
-- ''FK Bodø/Glimt''; matches agrees on the accents -- there is NO mojibake,
-- the earlier ''M?nchen'' was only a console mis-render). The names differ by
-- NAMING, not encoding: a prefix (''FC Bayern München'' vs ''Bayern
-- München''), a suffix (''Villarreal CF'' vs ''Villarreal''), or a short form
-- (''Inter'' vs ''Inter Milan''). The resolver joins by EXACT string, so each
-- side must match byte-for-byte -- which is why this seed was generated from
-- the two tables, never hand-typed. Names absent from sofifa FC25 (Brazil,
-- Belgium, Japan... EA licensing) get no row and keep GLM-only strength --
-- there is nothing to alias them to.
-- INSERT OR IGNORE so re-seeding never fights a hand-added row.
INSERT OR IGNORE INTO team_rating_aliases (sofifa_name, club) VALUES
    ('AEK Athens', 'AEK'),
    ('Ajax', 'Ajax Amsterdam'),
    ('Aston Villa', 'Aston Villa F.C'),
    ('Atalanta', 'Atalanta B.C'),
    ('Athletic Club', 'Athletic Bilbao'),
    ('FC Bayern München', 'Bayern München'),
    ('SL Benfica', 'Benfica Lisbon'),
    ('FK Bodø/Glimt', 'Bodo Glimt'),
    ('AFC Bournemouth', 'Bournemouth'),
    ('Sporting Clube de Braga', 'Braga F.C.'),
    ('RC Celta', 'Celta Vigo'),
    ('Club Brugge KV', 'Club Brugge'),
    ('Crystal Palace', 'Crystal Palace F.C'),
    ('Czechia', 'Czech Republic'),
    ('Fenerbahçe SK', 'Fenerbahce Istanbul'),
    ('Galatasaray SK', 'Galatasaray'),
    ('TSG 1899 Hoffenheim', 'Hoffenheim'),
    ('Inter', 'Inter Milan'),
    ('Juventus', 'Juventus Turin'),
    ('RB Leipzig', 'Leipzig'),
    ('Bayer 04 Leverkusen', 'Leverkusen'),
    ('Olympique de Marseille', 'Marseille'),
    ('Newcastle United', 'Newcastle Utd'),
    ('Nottingham Forest', 'Nottingham Forest F.C'),
    ('Olympiacos FC', 'Olympiacos'),
    ('Panathinaikos FC', 'Panathinaikos'),
    ('Paris Saint-Germain', 'PSG'),
    ('PSV', 'PSV Eindhoven'),
    ('Rangers FC', 'Rangers'),
    ('SK Rapid', 'Rapid Wien'),
    ('Real Betis Balompié', 'Real Betis'),
    ('SC Freiburg', 'S.C Freiburg'),
    ('Sporting CP', 'Sporting C.P'),
    ('Napoli', 'SSC Napoli'),
    ('VfB Stuttgart', 'Stuttgart'),
    ('Tottenham Hotspur', 'Tottenham'),
    ('United States', 'U.S.A'),
    ('Villarreal CF', 'Villarreal');
