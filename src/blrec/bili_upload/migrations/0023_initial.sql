ALTER TABLE recording_parts
ADD COLUMN upload_excluded_reason TEXT;

ALTER TABLE recording_parts
ADD COLUMN upload_probe_attempt INTEGER NOT NULL DEFAULT 0 CHECK (
    upload_probe_attempt >= 0
);
