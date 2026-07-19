-- Pick screen (docs/PICK_SCREEN.md): a rule-based veto layer ANNOTATING
-- served picks — it never modifies pick/tier/confidence, so regen_agrees
-- keeps grading the unscreened pick and stays a λ-honesty canary.
--
-- Columns, not side tables (unlike 003's x12 split): the screen means the
-- same thing on both prediction tables, and every consumer already selects
-- p.*. NULL = row predates the feature, screen was off, or the row carries
-- no pick (nothing to veto).

ALTER TABLE predictions     ADD COLUMN screen_pass INTEGER;
ALTER TABLE predictions     ADD COLUMN screen_reason TEXT;
ALTER TABLE predictions_x12 ADD COLUMN screen_pass INTEGER;
ALTER TABLE predictions_x12 ADD COLUMN screen_reason TEXT;
