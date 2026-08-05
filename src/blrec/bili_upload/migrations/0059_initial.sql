ALTER TABLE vainglory_archive_syncs
ADD COLUMN season_started_at INTEGER;

ALTER TABLE vainglory_archive_syncs
ADD COLUMN season_ended_at INTEGER;

ALTER TABLE vainglory_publications
ADD COLUMN priority INTEGER NOT NULL DEFAULT 0
CHECK (priority IN (0,1));

CREATE TABLE vainglory_match_suppressions (
    part_id INTEGER NOT NULL
        REFERENCES recording_parts(id) ON DELETE CASCADE,
    at_ms INTEGER NOT NULL CHECK (at_ms >= 0),
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    PRIMARY KEY(part_id,at_ms)
);

CREATE TABLE vainglory_match_rerun_jobs (
    match_id INTEGER PRIMARY KEY
        REFERENCES vainglory_matches(id) ON DELETE CASCADE,
    state TEXT NOT NULL
        CHECK (state IN ('pending','running','failed')),
    error TEXT,
    requested_at INTEGER NOT NULL CHECK (requested_at > 0),
    started_at INTEGER,
    completed_at INTEGER,
    updated_at INTEGER NOT NULL CHECK (updated_at >= requested_at)
);

CREATE INDEX vainglory_match_rerun_jobs_state_idx
ON vainglory_match_rerun_jobs(state,requested_at,match_id);
