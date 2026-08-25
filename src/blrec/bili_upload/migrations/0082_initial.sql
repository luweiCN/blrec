ALTER TABLE vainglory_matches
ADD COLUMN afk_detection_version INTEGER NOT NULL DEFAULT 0
CHECK (afk_detection_version >= 0);

CREATE TABLE vainglory_afk_backfill_jobs (
    match_id INTEGER PRIMARY KEY
        REFERENCES vainglory_matches(id) ON DELETE CASCADE,
    detection_version INTEGER NOT NULL CHECK (detection_version > 0),
    state TEXT NOT NULL CHECK (
        state IN ('pending','running','completed','failed')
    ),
    error TEXT CHECK (error IS NULL OR length(error) <= 500),
    requested_at INTEGER NOT NULL CHECK (requested_at > 0),
    started_at INTEGER CHECK (started_at IS NULL OR started_at >= requested_at),
    completed_at INTEGER CHECK (
        completed_at IS NULL OR completed_at >= requested_at
    ),
    updated_at INTEGER NOT NULL CHECK (updated_at >= requested_at)
);

CREATE INDEX vainglory_afk_backfill_jobs_claim_idx
ON vainglory_afk_backfill_jobs(state,detection_version,requested_at,match_id);

INSERT INTO vainglory_afk_backfill_jobs(
    match_id,detection_version,state,error,requested_at,started_at,
    completed_at,updated_at
)
SELECT id,1,'pending',NULL,created_at,NULL,NULL,created_at
FROM vainglory_matches
WHERE result_frame_path IS NOT NULL;
