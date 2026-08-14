ALTER TABLE vainglory_matches
ADD COLUMN analysis_state TEXT NOT NULL DEFAULT 'final'
CHECK (analysis_state IN ('provisional','final'));

CREATE INDEX vainglory_matches_analysis_state_idx
ON vainglory_matches(analysis_state,session_id,result_part_id,result_at_ms);

CREATE TABLE vainglory_live_analysis_state (
    part_id INTEGER PRIMARY KEY
        REFERENCES recording_parts(id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL
        REFERENCES recording_sessions(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active','closed')),
    next_sample_at INTEGER NOT NULL CHECK (next_sample_at >= 0),
    last_sample_at INTEGER,
    last_observed_at_ms INTEGER CHECK (
        last_observed_at_ms IS NULL OR last_observed_at_ms >= 0
    ),
    last_match_flow_label TEXT NOT NULL DEFAULT '',
    last_match_flow_confidence REAL NOT NULL DEFAULT 0
        CHECK (last_match_flow_confidence BETWEEN 0 AND 1),
    last_in_match_at_ms INTEGER CHECK (
        last_in_match_at_ms IS NULL OR last_in_match_at_ms >= 0
    ),
    last_hero_lineup_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(last_hero_lineup_json)),
    sample_count INTEGER NOT NULL DEFAULT 0 CHECK (sample_count >= 0),
    fine_scan_count INTEGER NOT NULL DEFAULT 0 CHECK (fine_scan_count >= 0),
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_until INTEGER,
    last_error TEXT,
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
    UNIQUE(session_id,part_id),
    CHECK ((lease_owner IS NULL)=(lease_until IS NULL))
);

CREATE INDEX vainglory_live_analysis_due_idx
ON vainglory_live_analysis_state(state,next_sample_at,lease_until,part_id);

CREATE TABLE vainglory_live_analysis_windows (
    id INTEGER PRIMARY KEY,
    part_id INTEGER NOT NULL
        REFERENCES recording_parts(id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL
        REFERENCES recording_sessions(id) ON DELETE CASCADE,
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK (end_ms > start_ms),
    focus_ms INTEGER CHECK (
        focus_ms IS NULL OR (focus_ms >= start_ms AND focus_ms <= end_ms)
    ),
    mode TEXT NOT NULL DEFAULT 'unknown'
        CHECK (mode IN ('3v3','aram','5v5','unknown')),
    available_at INTEGER NOT NULL CHECK (available_at > 0),
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','running','ready','failed')),
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_until INTEGER,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    match_count INTEGER NOT NULL DEFAULT 0 CHECK (match_count >= 0),
    error TEXT,
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    completed_at INTEGER,
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
    UNIQUE(part_id,start_ms,end_ms),
    CHECK ((lease_owner IS NULL)=(lease_until IS NULL))
);

CREATE INDEX vainglory_live_analysis_windows_queue_idx
ON vainglory_live_analysis_windows(state,available_at,created_at,id);

CREATE TABLE vainglory_live_observations (
    id INTEGER PRIMARY KEY,
    part_id INTEGER NOT NULL
        REFERENCES recording_parts(id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL
        REFERENCES recording_sessions(id) ON DELETE CASCADE,
    observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
    stage INTEGER NOT NULL,
    stage_confidence REAL NOT NULL CHECK (stage_confidence BETWEEN 0 AND 1),
    match_flow_label TEXT NOT NULL,
    match_flow_confidence REAL NOT NULL
        CHECK (match_flow_confidence BETWEEN 0 AND 1),
    hero_select_label TEXT NOT NULL,
    hero_select_confidence REAL NOT NULL
        CHECK (hero_select_confidence BETWEEN 0 AND 1),
    match_mode_label TEXT NOT NULL,
    match_mode_confidence REAL NOT NULL
        CHECK (match_mode_confidence BETWEEN 0 AND 1),
    result_confidence REAL NOT NULL CHECK (result_confidence BETWEEN 0 AND 1),
    hero_lineup_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(hero_lineup_json)),
    model_version TEXT NOT NULL DEFAULT '',
    selected_for_review INTEGER NOT NULL DEFAULT 0
        CHECK (selected_for_review IN (0,1)),
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    UNIQUE(part_id,observed_at_ms)
);

CREATE INDEX vainglory_live_observations_part_idx
ON vainglory_live_observations(part_id,observed_at_ms);
