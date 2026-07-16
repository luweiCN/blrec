ALTER TABLE recording_parts
ADD COLUMN media_index_state TEXT NOT NULL DEFAULT 'pending'
CHECK (media_index_state IN (
    'pending','indexing','ready','failed','not_required'
));

ALTER TABLE recording_parts
ADD COLUMN media_index_error TEXT;

ALTER TABLE recording_parts
ADD COLUMN media_index_progress REAL NOT NULL DEFAULT 0
CHECK (media_index_progress BETWEEN 0 AND 1);

ALTER TABLE recording_parts
ADD COLUMN media_index_updated_at INTEGER;

ALTER TABLE recording_parts
ADD COLUMN media_index_owner TEXT;

ALTER TABLE recording_parts
ADD COLUMN media_index_lease_until INTEGER;

ALTER TABLE recording_parts
ADD COLUMN media_index_attempt INTEGER NOT NULL DEFAULT 0
CHECK (media_index_attempt >= 0);

CREATE INDEX recording_parts_media_index_idx
ON recording_parts(media_index_state,artifact_state,media_index_lease_until,id);
