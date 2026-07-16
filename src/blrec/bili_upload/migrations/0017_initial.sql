ALTER TABLE upload_jobs
ADD COLUMN submission_verification_state TEXT NOT NULL DEFAULT 'pending' CHECK (
    submission_verification_state IN (
        'pending','passed','different','partial','failed'
    )
);

ALTER TABLE upload_jobs
ADD COLUMN submission_verified_at INTEGER CHECK (
    submission_verified_at IS NULL OR submission_verified_at > 0
);

ALTER TABLE upload_jobs
ADD COLUMN submission_verification_json TEXT;
