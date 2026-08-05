ALTER TABLE vainglory_publications
ADD COLUMN comment_cleanup_state TEXT NOT NULL DEFAULT 'confirmed'
CHECK (comment_cleanup_state IN ('prepared','in_flight','confirmed'));

CREATE TABLE vainglory_manual_match_markers (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL
        REFERENCES recording_sessions(id) ON DELETE CASCADE,
    part_id INTEGER NOT NULL
        REFERENCES recording_parts(id) ON DELETE CASCADE,
    at_ms INTEGER NOT NULL CHECK (at_ms >= 0),
    source TEXT NOT NULL CHECK (
        source IN ('browser_extension','dashboard')
    ),
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
    UNIQUE(part_id,at_ms)
);

CREATE INDEX vainglory_manual_match_markers_part_idx
ON vainglory_manual_match_markers(part_id,at_ms);

CREATE TABLE vainglory_match_overrides (
    id INTEGER PRIMARY KEY,
    part_id INTEGER NOT NULL
        REFERENCES recording_parts(id) ON DELETE CASCADE,
    result_at_ms INTEGER NOT NULL CHECK (result_at_ms >= 0),
    payload_json TEXT NOT NULL CHECK (length(payload_json) > 1),
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
    UNIQUE(part_id,result_at_ms)
);

CREATE INDEX vainglory_match_overrides_part_idx
ON vainglory_match_overrides(part_id,result_at_ms);
