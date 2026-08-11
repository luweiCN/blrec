UPDATE vainglory_archive_imports
SET quota_day=NULL;

UPDATE vainglory_archive_syncs
SET quota_day=NULL,
    daily_used=0;

UPDATE vainglory_archive_syncs
SET state=CASE state WHEN 'ready' THEN 'running' ELSE state END,
    discovery_complete=0,
    completed_at=CASE state WHEN 'ready' THEN NULL ELSE completed_at END,
    updated_at=MAX(updated_at,CAST(strftime('%s','now') AS INTEGER))
WHERE state IN ('discovering','running','ready');
