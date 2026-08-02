ALTER TABLE archive_migration_jobs
ADD COLUMN operator_paused INTEGER NOT NULL DEFAULT 0
CHECK (operator_paused IN (0,1));

ALTER TABLE archive_migration_jobs
ADD COLUMN daily_limit INTEGER NOT NULL DEFAULT 20
CHECK (daily_limit BETWEEN 1 AND 500);

ALTER TABLE archive_migration_jobs
ADD COLUMN quota_day TEXT
CHECK (quota_day IS NULL OR length(quota_day)=10);

ALTER TABLE archive_migration_jobs
ADD COLUMN daily_used INTEGER NOT NULL DEFAULT 0
CHECK (daily_used >= 0);

ALTER TABLE archive_migration_items
ADD COLUMN quota_day TEXT
CHECK (quota_day IS NULL OR length(quota_day)=10);

ALTER TABLE vainglory_archive_syncs
ADD COLUMN operator_paused INTEGER NOT NULL DEFAULT 0
CHECK (operator_paused IN (0,1));

ALTER TABLE vainglory_archive_syncs
ADD COLUMN daily_limit INTEGER NOT NULL DEFAULT 20
CHECK (daily_limit BETWEEN 1 AND 500);

ALTER TABLE vainglory_archive_syncs
ADD COLUMN quota_day TEXT
CHECK (quota_day IS NULL OR length(quota_day)=10);

ALTER TABLE vainglory_archive_syncs
ADD COLUMN daily_used INTEGER NOT NULL DEFAULT 0
CHECK (daily_used >= 0);

ALTER TABLE vainglory_archive_syncs
ADD COLUMN next_page INTEGER NOT NULL DEFAULT 1
CHECK (next_page > 0);

ALTER TABLE vainglory_archive_syncs
ADD COLUMN discovery_complete INTEGER NOT NULL DEFAULT 0
CHECK (discovery_complete IN (0,1));

ALTER TABLE vainglory_archive_syncs
ADD COLUMN last_page_identity TEXT;

UPDATE vainglory_archive_syncs SET discovery_complete=1;

ALTER TABLE vainglory_archive_imports
ADD COLUMN quota_day TEXT
CHECK (quota_day IS NULL OR length(quota_day)=10);
