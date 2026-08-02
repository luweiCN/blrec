CREATE TABLE vainglory_scan_jobs (
    session_id INTEGER PRIMARY KEY
        REFERENCES recording_sessions(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (
        state IN ('pending','analyzing','ready','failed')
    ),
    progress REAL NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
    algorithm_version INTEGER NOT NULL CHECK (algorithm_version > 0),
    match_count INTEGER NOT NULL DEFAULT 0 CHECK (match_count >= 0),
    error TEXT,
    requested_at INTEGER NOT NULL CHECK (requested_at > 0),
    started_at INTEGER,
    completed_at INTEGER,
    updated_at INTEGER NOT NULL CHECK (updated_at >= requested_at),
    CHECK (
        (state='failed' AND error IS NOT NULL AND length(error) > 0) OR
        (state!='failed' AND error IS NULL)
    ),
    CHECK (started_at IS NULL OR started_at >= requested_at),
    CHECK (completed_at IS NULL OR completed_at >= requested_at)
);

CREATE INDEX vainglory_scan_jobs_state_idx
ON vainglory_scan_jobs(state,requested_at,session_id);

CREATE TABLE vainglory_heroes (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE CHECK (
        length(fingerprint) BETWEEN 16 AND 64
        AND fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    thumbnail_png BLOB NOT NULL CHECK (length(thumbnail_png) > 0),
    label TEXT NOT NULL DEFAULT '' CHECK (
        label=trim(label) AND length(label) <= 80
    ),
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at)
);

CREATE INDEX vainglory_heroes_label_idx
ON vainglory_heroes(label COLLATE NOCASE,id);

CREATE TABLE vainglory_matches (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL
        REFERENCES vainglory_scan_jobs(session_id) ON DELETE CASCADE,
    result_part_id INTEGER NOT NULL
        REFERENCES recording_parts(id) ON DELETE CASCADE,
    result_at_ms INTEGER NOT NULL CHECK (result_at_ms >= 0),
    duration_seconds INTEGER CHECK (duration_seconds > 0),
    result_text TEXT NOT NULL CHECK (length(result_text) <= 32),
    end_reason TEXT NOT NULL CHECK (
        end_reason IN ('normal','surrender','unknown')
    ),
    left_color TEXT NOT NULL CHECK (left_color IN ('teal','orange')),
    right_color TEXT NOT NULL CHECK (right_color IN ('teal','orange')),
    winner_side TEXT NOT NULL CHECK (
        winner_side IN ('left','right','unknown')
    ),
    left_kills INTEGER CHECK (left_kills >= 0),
    right_kills INTEGER CHECK (right_kills >= 0),
    left_economy INTEGER CHECK (left_economy >= 0),
    right_economy INTEGER CHECK (right_economy >= 0),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    UNIQUE(result_part_id,result_at_ms),
    CHECK (left_color != right_color)
);

CREATE INDEX vainglory_matches_list_idx
ON vainglory_matches(created_at DESC,id DESC);

CREATE INDEX vainglory_matches_session_idx
ON vainglory_matches(session_id,result_part_id,result_at_ms);

CREATE TABLE vainglory_match_players (
    match_id INTEGER NOT NULL
        REFERENCES vainglory_matches(id) ON DELETE CASCADE,
    side TEXT NOT NULL CHECK (side IN ('left','right')),
    slot INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 3),
    player_name TEXT NOT NULL CHECK (length(player_name) <= 80),
    normalized_name TEXT NOT NULL CHECK (length(normalized_name) <= 80),
    hero_id INTEGER REFERENCES vainglory_heroes(id) ON DELETE SET NULL,
    kills INTEGER CHECK (kills >= 0),
    deaths INTEGER CHECK (deaths >= 0),
    assists INTEGER CHECK (assists >= 0),
    economy INTEGER CHECK (economy >= 0),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    PRIMARY KEY(match_id,side,slot)
);

CREATE INDEX vainglory_match_players_name_idx
ON vainglory_match_players(normalized_name,match_id);

CREATE INDEX vainglory_match_players_hero_idx
ON vainglory_match_players(hero_id,match_id);
