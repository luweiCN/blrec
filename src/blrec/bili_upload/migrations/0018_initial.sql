ALTER TABLE recording_sessions
ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'live'
CHECK (source_kind IN ('live','highlight'));

ALTER TABLE recording_parts
ADD COLUMN timeline_start_at_ms INTEGER
CHECK (timeline_start_at_ms IS NULL OR timeline_start_at_ms > 0);

CREATE TABLE highlight_markers (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL CHECK (room_id > 0),
    observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms > 0),
    player_delay_ms INTEGER NOT NULL DEFAULT 0
        CHECK (player_delay_ms BETWEEN 0 AND 300000),
    content_at_ms INTEGER NOT NULL CHECK (content_at_ms > 0),
    title TEXT NOT NULL,
    anchor_name TEXT NOT NULL,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 1000),
    source TEXT NOT NULL CHECK (source IN ('web','browser_extension')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX highlight_markers_room_time_idx
ON highlight_markers(room_id,content_at_ms,id);

CREATE TABLE highlight_clips (
    id INTEGER PRIMARY KEY,
    marker_id INTEGER REFERENCES highlight_markers(id) ON DELETE SET NULL,
    room_id INTEGER NOT NULL CHECK (room_id > 0),
    source_session_id INTEGER
        REFERENCES recording_sessions(id) ON DELETE SET NULL,
    upload_session_id INTEGER UNIQUE
        REFERENCES recording_sessions(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    requested_start_ms INTEGER NOT NULL CHECK (requested_start_ms >= 0),
    requested_end_ms INTEGER NOT NULL CHECK (requested_end_ms > requested_start_ms),
    actual_start_ms INTEGER CHECK (actual_start_ms IS NULL OR actual_start_ms >= 0),
    actual_end_ms INTEGER CHECK (
        actual_end_ms IS NULL OR
        (actual_start_ms IS NOT NULL AND actual_end_ms > actual_start_ms)
    ),
    output_video_path TEXT,
    output_xml_path TEXT,
    state TEXT NOT NULL CHECK (
        state IN ('queued','processing','ready','failed','cancelled')
    ),
    keyframe_confirmation_required INTEGER NOT NULL DEFAULT 0
        CHECK (keyframe_confirmation_required IN (0,1)),
    keyframe_confirmed INTEGER NOT NULL DEFAULT 0
        CHECK (keyframe_confirmed IN (0,1)),
    error_message TEXT,
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_until INTEGER,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX highlight_clips_claim_idx
ON highlight_clips(state,next_attempt_at,priority,id);

CREATE TABLE highlight_clip_sources (
    clip_id INTEGER NOT NULL REFERENCES highlight_clips(id) ON DELETE CASCADE,
    part_id INTEGER NOT NULL REFERENCES recording_parts(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    requested_start_ms INTEGER NOT NULL CHECK (requested_start_ms >= 0),
    requested_end_ms INTEGER NOT NULL CHECK (requested_end_ms > requested_start_ms),
    actual_start_ms INTEGER CHECK (actual_start_ms IS NULL OR actual_start_ms >= 0),
    actual_end_ms INTEGER CHECK (
        actual_end_ms IS NULL OR
        (actual_start_ms IS NOT NULL AND actual_end_ms > actual_start_ms)
    ),
    PRIMARY KEY (clip_id,ordinal),
    UNIQUE (clip_id,part_id)
);

CREATE INDEX highlight_clip_sources_part_idx
ON highlight_clip_sources(part_id,clip_id);
