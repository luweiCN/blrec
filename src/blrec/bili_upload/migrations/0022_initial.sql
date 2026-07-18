ALTER TABLE upload_jobs
ADD COLUMN preupload_finalized INTEGER NOT NULL DEFAULT 1 CHECK (
    preupload_finalized IN (0,1)
);
