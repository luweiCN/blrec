ALTER TABLE upload_jobs
ADD COLUMN repair_state TEXT NOT NULL DEFAULT 'idle' CHECK (
    repair_state IN (
        'idle','queued','checking','reuploading','editing','waiting_review',
        'not_needed','completed','failed','unknown_outcome'
    )
);

ALTER TABLE upload_jobs
ADD COLUMN repair_message TEXT;

ALTER TABLE upload_jobs
ADD COLUMN repair_error TEXT;

ALTER TABLE upload_jobs
ADD COLUMN repair_attempt INTEGER NOT NULL DEFAULT 0 CHECK (repair_attempt >= 0);

ALTER TABLE upload_jobs
ADD COLUMN repair_requested_at INTEGER CHECK (
    repair_requested_at IS NULL OR repair_requested_at > 0
);

ALTER TABLE upload_jobs
ADD COLUMN repair_completed_at INTEGER CHECK (
    repair_completed_at IS NULL OR repair_completed_at > 0
);

ALTER TABLE upload_parts
ADD COLUMN transcode_state TEXT NOT NULL DEFAULT 'unknown' CHECK (
    transcode_state IN ('unknown','ready','processing','failed')
);

ALTER TABLE upload_parts
ADD COLUMN transcode_fail_code INTEGER;

ALTER TABLE upload_parts
ADD COLUMN transcode_fail_desc TEXT;

CREATE INDEX upload_jobs_repair_idx
ON upload_jobs(repair_state, repair_requested_at, id);
