-- Strong-pick Telegram alert ledger (docs/ALERTS.md): one row per pick already
-- broadcast to the channel, so the `alert` job never re-sends the same call.
-- Operational/bot state, not model output — it lives in its own table and the
-- prediction/settlement loops never read or write it.
--
-- The (event_id, line, selection) key IS the "only re-alert when the pick
-- changes" rule: a re-price that keeps the same strong line + side collides and
-- stays quiet; a flipped side or a newly-strong line is a fresh key and fires.

CREATE TABLE alerts_sent (
    event_id   TEXT NOT NULL,
    line       REAL NOT NULL,
    selection  TEXT NOT NULL,            -- over | under
    tier       TEXT NOT NULL,            -- served tier at send time (e.g. strong)
    confidence REAL NOT NULL,            -- model prob of the picked side at send
    message_id INTEGER,                  -- Telegram message id (null if unknown)
    sent_at    TEXT NOT NULL,            -- ISO-8601 UTC
    PRIMARY KEY (event_id, line, selection)
);
