CREATE TABLE replay_visibility_checks (
    bvid TEXT PRIMARY KEY CHECK (
        length(bvid) BETWEEN 6 AND 20
        AND substr(bvid,1,2)='BV'
        AND bvid NOT GLOB '*[^0-9A-Za-z]*'
    ),
    state TEXT NOT NULL CHECK (
        state IN ('pending','checking','public','unavailable')
    ),
    checked_at INTEGER CHECK (checked_at IS NULL OR checked_at >= 0),
    expires_at INTEGER CHECK (expires_at IS NULL OR expires_at >= 0),
    requested_at INTEGER NOT NULL CHECK (requested_at >= 0),
    claimed_at INTEGER CHECK (claimed_at IS NULL OR claimed_at >= 0),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at INTEGER NOT NULL CHECK (next_attempt_at >= 0),
    last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 500),
    updated_at INTEGER NOT NULL CHECK (updated_at >= 0)
);

CREATE INDEX replay_visibility_queue_idx
ON replay_visibility_checks(state,next_attempt_at,requested_at);
