ALTER TABLE archive_migration_items
ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0
CHECK (attempt_count >= 0);

UPDATE archive_migration_items
SET attempt_count=1
WHERE state!='queued';
