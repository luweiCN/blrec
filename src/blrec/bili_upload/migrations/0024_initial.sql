ALTER TABLE highlight_clips
ADD COLUMN file_size_bytes INTEGER
CHECK (file_size_bytes IS NULL OR file_size_bytes >= 0);
