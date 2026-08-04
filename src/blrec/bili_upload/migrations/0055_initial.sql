ALTER TABLE vainglory_archive_imports
ADD COLUMN recording_started_at INTEGER
    CHECK (recording_started_at IS NULL OR recording_started_at > 0);

CREATE INDEX vainglory_archive_imports_recording_time_idx
ON vainglory_archive_imports(
    account_id,recording_started_at DESC,id DESC
);
