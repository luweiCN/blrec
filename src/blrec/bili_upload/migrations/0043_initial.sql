ALTER TABLE vainglory_publications
ADD COLUMN needs_refresh INTEGER NOT NULL DEFAULT 1
CHECK (needs_refresh IN (0,1));

CREATE INDEX vainglory_publications_refresh_idx
ON vainglory_publications(needs_refresh,state,id);

CREATE TABLE vainglory_ocr_jobs (
    part_id INTEGER PRIMARY KEY
        REFERENCES recording_parts(id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL
        REFERENCES recording_sessions(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('pending','running')),
    video_duration_ms INTEGER NOT NULL CHECK (video_duration_ms > 0),
    candidate_times_json TEXT NOT NULL CHECK (
        length(candidate_times_json) BETWEEN 3 AND 100000
    ),
    candidate_count INTEGER NOT NULL CHECK (candidate_count > 0),
    requested_at INTEGER NOT NULL CHECK (requested_at > 0),
    started_at INTEGER,
    updated_at INTEGER NOT NULL CHECK (updated_at >= requested_at),
    CHECK (started_at IS NULL OR started_at >= requested_at)
);

CREATE INDEX vainglory_ocr_jobs_state_idx
ON vainglory_ocr_jobs(state,requested_at,part_id);

CREATE INDEX vainglory_ocr_jobs_session_idx
ON vainglory_ocr_jobs(session_id,state,part_id);
