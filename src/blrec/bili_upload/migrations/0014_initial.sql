ALTER TABLE upload_jobs
ADD COLUMN operator_paused INTEGER NOT NULL DEFAULT 0 CHECK (
    operator_paused IN (0,1)
);

ALTER TABLE upload_jobs
ADD COLUMN operator_resume_state TEXT CHECK (
    operator_resume_state IS NULL OR operator_resume_state IN (
        'ready','uploading','submitting'
    )
);

ALTER TABLE recording_sessions
ADD COLUMN deletion_state TEXT NOT NULL DEFAULT 'none' CHECK (
    deletion_state IN ('none','requested','deleting','failed')
);

ALTER TABLE recording_sessions
ADD COLUMN deletion_error TEXT;

ALTER TABLE recording_sessions
ADD COLUMN deletion_requested_at INTEGER CHECK (
    deletion_requested_at IS NULL OR deletion_requested_at > 0
);

CREATE INDEX recording_sessions_deletion_idx
ON recording_sessions(deletion_state, deletion_requested_at, id);
