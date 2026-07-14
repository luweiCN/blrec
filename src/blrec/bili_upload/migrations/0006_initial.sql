ALTER TABLE recording_sessions
ADD COLUMN title TEXT NOT NULL DEFAULT '';

ALTER TABLE recording_sessions
ADD COLUMN cover_url TEXT NOT NULL DEFAULT '';

ALTER TABLE recording_sessions
ADD COLUMN cover_path TEXT;

ALTER TABLE recording_sessions
ADD COLUMN anchor_uid INTEGER;

ALTER TABLE recording_sessions
ADD COLUMN anchor_name TEXT NOT NULL DEFAULT '';

ALTER TABLE recording_sessions
ADD COLUMN area_id INTEGER;

ALTER TABLE recording_sessions
ADD COLUMN area_name TEXT NOT NULL DEFAULT '';

ALTER TABLE recording_sessions
ADD COLUMN parent_area_id INTEGER;

ALTER TABLE recording_sessions
ADD COLUMN parent_area_name TEXT NOT NULL DEFAULT '';

ALTER TABLE recording_sessions
ADD COLUMN live_end_time INTEGER;

ALTER TABLE recording_parts
ADD COLUMN record_end_time INTEGER;

ALTER TABLE recording_parts
ADD COLUMN record_duration_seconds INTEGER CHECK (
    record_duration_seconds IS NULL OR record_duration_seconds >= 0
);

ALTER TABLE recording_parts
ADD COLUMN file_size_bytes INTEGER CHECK (
    file_size_bytes IS NULL OR file_size_bytes >= 0
);

ALTER TABLE recording_parts
ADD COLUMN danmaku_count INTEGER NOT NULL DEFAULT 0 CHECK (danmaku_count >= 0);
