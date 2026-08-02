ALTER TABLE vainglory_scan_jobs
ADD COLUMN stats_included INTEGER NOT NULL DEFAULT 1
CHECK (stats_included IN (0,1));
