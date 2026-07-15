ALTER TABLE room_upload_policies
ADD COLUMN retention_mode TEXT NOT NULL DEFAULT 'submitted' CHECK (
    retention_mode IN (
        'never','upload_completed','submitted','approved','capacity'
    )
);

ALTER TABLE room_upload_policies
ADD COLUMN retention_days INTEGER NOT NULL DEFAULT 5 CHECK (
    retention_days BETWEEN 0 AND 3650
);

ALTER TABLE upload_jobs
ADD COLUMN upload_completed_at INTEGER CHECK (
    upload_completed_at IS NULL OR upload_completed_at > 0
);

ALTER TABLE upload_jobs
ADD COLUMN submitted_at INTEGER CHECK (
    submitted_at IS NULL OR submitted_at > 0
);

ALTER TABLE upload_jobs
ADD COLUMN approved_at INTEGER CHECK (
    approved_at IS NULL OR approved_at > 0
);

ALTER TABLE recording_parts
ADD COLUMN video_deleted_at INTEGER CHECK (
    video_deleted_at IS NULL OR video_deleted_at > 0
);

ALTER TABLE recording_parts
ADD COLUMN video_delete_reason TEXT;

ALTER TABLE recording_parts
ADD COLUMN video_delete_error TEXT;

UPDATE upload_jobs
SET upload_completed_at=updated_at
WHERE upload_completed_at IS NULL AND state IN (
    'submitting','waiting_review','approved','rejected','completed'
);

UPDATE upload_jobs
SET submitted_at=updated_at
WHERE submitted_at IS NULL AND submit_state='confirmed'
  AND aid IS NOT NULL AND bvid IS NOT NULL;

UPDATE upload_jobs
SET approved_at=updated_at
WHERE approved_at IS NULL AND state IN ('approved','completed');

CREATE INDEX recording_parts_retention_idx
ON recording_parts(video_deleted_at, session_id, part_index);
