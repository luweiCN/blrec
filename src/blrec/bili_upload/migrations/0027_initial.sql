ALTER TABLE highlight_clips
ADD COLUMN inspection_json TEXT;

ALTER TABLE highlight_clips
ADD COLUMN source_fingerprint_json TEXT;

ALTER TABLE highlight_clips
ADD COLUMN idempotency_key TEXT;

CREATE UNIQUE INDEX highlight_clips_idempotency_idx
ON highlight_clips(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE TABLE highlight_inspections (
    operation_id TEXT PRIMARY KEY,
    session_id INTEGER NOT NULL CHECK (session_id > 0),
    requested_start_ms INTEGER NOT NULL CHECK (requested_start_ms >= 0),
    requested_end_ms INTEGER NOT NULL CHECK (
        requested_end_ms > requested_start_ms
    ),
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (
        state IN ('accepted','running','succeeded','failed')
    ),
    active_durations_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    error_code TEXT,
    fingerprint_json TEXT,
    claim_key_hash TEXT,
    token_hash TEXT UNIQUE,
    token_expires_at INTEGER,
    token_consumed_at INTEGER,
    terminal_expires_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    CHECK (
        (state IN ('accepted','running') AND terminal_expires_at IS NULL) OR
        (state IN ('succeeded','failed') AND terminal_expires_at IS NOT NULL)
    )
);

CREATE INDEX highlight_inspections_admission_idx
ON highlight_inspections(state,created_at,operation_id);

CREATE INDEX highlight_inspections_reuse_idx
ON highlight_inspections(
    state,session_id,requested_start_ms,requested_end_ms,fingerprint_json
);

CREATE INDEX highlight_inspections_expiry_idx
ON highlight_inspections(state,terminal_expires_at,operation_id);
