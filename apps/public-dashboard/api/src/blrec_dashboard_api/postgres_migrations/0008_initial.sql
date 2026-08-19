CREATE TABLE replay_visibility_checks (
    bvid TEXT PRIMARY KEY CHECK (bvid ~ '^BV[0-9A-Za-z]{4,18}$'),
    state TEXT NOT NULL CHECK (
        state IN ('pending','checking','public','unavailable')
    ),
    checked_at BIGINT CHECK (checked_at IS NULL OR checked_at >= 0),
    expires_at BIGINT CHECK (expires_at IS NULL OR expires_at >= 0),
    requested_at BIGINT NOT NULL CHECK (requested_at >= 0),
    claimed_at BIGINT CHECK (claimed_at IS NULL OR claimed_at >= 0),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at BIGINT NOT NULL CHECK (next_attempt_at >= 0),
    last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 500),
    updated_at BIGINT NOT NULL CHECK (updated_at >= 0)
);

CREATE INDEX replay_visibility_queue_idx
ON replay_visibility_checks(state,next_attempt_at,requested_at);
