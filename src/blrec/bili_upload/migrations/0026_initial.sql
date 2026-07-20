ALTER TABLE recording_sessions
ADD COLUMN cancellation_generation INTEGER NOT NULL DEFAULT 0
CHECK (cancellation_generation >= 0);

UPDATE recording_sessions
SET cancellation_generation=1
WHERE deletion_state!='none' AND cancellation_generation=0;

ALTER TABLE highlight_clips
ADD COLUMN cancellation_generation INTEGER NOT NULL DEFAULT 0
CHECK (cancellation_generation >= 0);

ALTER TABLE highlight_clips
ADD COLUMN deletion_state TEXT NOT NULL DEFAULT 'none'
CHECK (deletion_state IN ('none','requested','quiescing','deleting','failed'));

ALTER TABLE highlight_clips
ADD COLUMN deletion_error TEXT;

ALTER TABLE highlight_clips
ADD COLUMN deletion_requested_at INTEGER
CHECK (deletion_requested_at IS NULL OR deletion_requested_at > 0);

CREATE INDEX highlight_clips_deletion_idx
ON highlight_clips(deletion_state,deletion_requested_at,id);

CREATE TABLE local_deletion_items (
    id INTEGER PRIMARY KEY,
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('session','clip')),
    owner_id INTEGER NOT NULL CHECK (owner_id > 0),
    cancellation_generation INTEGER NOT NULL
        CHECK (cancellation_generation > 0),
    path TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','deleting','failed','done')),
    error TEXT,
    UNIQUE(owner_kind,owner_id,cancellation_generation,path)
);

CREATE INDEX local_deletion_items_state_idx
ON local_deletion_items(state,id);

CREATE TABLE owner_handoff_outcomes (
    id INTEGER PRIMARY KEY,
    owner_kind TEXT NOT NULL,
    owner_id INTEGER NOT NULL CHECK (owner_id > 0),
    side_effect_key TEXT NOT NULL,
    source_generation INTEGER NOT NULL CHECK (source_generation >= 0),
    outcome_state TEXT NOT NULL CHECK (
        outcome_state IN (
            'in_flight','confirmed_success','confirmed_failure',
            'unknown_terminal','cancelled_local'
        )
    ),
    outcome_json TEXT NOT NULL DEFAULT '{}',
    acknowledged_at INTEGER,
    CHECK (
        (outcome_state='in_flight' AND acknowledged_at IS NULL) OR
        (outcome_state!='in_flight' AND acknowledged_at IS NOT NULL)
    ),
    UNIQUE(owner_kind,owner_id,side_effect_key,source_generation)
);

CREATE INDEX owner_handoff_outcomes_ack_idx
ON owner_handoff_outcomes(owner_kind,owner_id,outcome_state,id);
