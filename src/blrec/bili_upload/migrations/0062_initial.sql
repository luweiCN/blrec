CREATE TEMP TABLE migration_0062_retry_parts (
    part_id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL
);

INSERT INTO migration_0062_retry_parts(part_id,session_id)
SELECT job.part_id,job.session_id
FROM vainglory_part_jobs job
JOIN recording_parts part ON part.id=job.part_id
JOIN recording_sessions session ON session.id=job.session_id
WHERE job.state='failed'
AND part.artifact_state='ready'
AND part.video_deleted_at IS NULL
AND session.deletion_state='none'
AND session.state NOT IN ('cancelled','skipped')
AND instr(COALESCE(session.title,''),'直播剪辑')=0
AND NOT EXISTS(
    SELECT 1 FROM vainglory_scan_suppressions suppression
    WHERE suppression.session_id=job.session_id
)
AND NOT EXISTS(
    SELECT 1 FROM upload_jobs upload
    WHERE upload.session_id=job.session_id
    AND instr(COALESCE(upload.policy_snapshot_json,''),'直播剪辑')>0
)
AND NOT EXISTS(
    SELECT 1 FROM archive_migration_items migration
    WHERE migration.session_id=job.session_id
    AND instr(COALESCE(migration.title,''),'直播剪辑')>0
)
AND NOT EXISTS(
    SELECT 1 FROM vainglory_archive_imports imported
    WHERE imported.session_id=job.session_id
    AND instr(COALESCE(imported.title,''),'直播剪辑')>0
);

UPDATE vainglory_archive_parts
SET state='queued',progress=0,error=NULL,
    updated_at=CAST(strftime('%s','now') AS INTEGER)
WHERE recording_part_id IN (
    SELECT part_id FROM migration_0062_retry_parts
);

UPDATE vainglory_archive_imports
SET state='analyzing',
    progress=CAST((
        SELECT COUNT(*) FROM vainglory_archive_parts ready_part
        WHERE ready_part.import_id=vainglory_archive_imports.id
        AND ready_part.state='ready'
    ) AS REAL) / MAX(1,page_count),
    completed_page_count=(
        SELECT COUNT(*) FROM vainglory_archive_parts ready_part
        WHERE ready_part.import_id=vainglory_archive_imports.id
        AND ready_part.state='ready'
    ),
    error=NULL,retryable=0,next_retry_at=NULL,
    updated_at=CAST(strftime('%s','now') AS INTEGER)
WHERE id IN (
    SELECT archive_part.import_id
    FROM vainglory_archive_parts archive_part
    JOIN migration_0062_retry_parts retry
    ON retry.part_id=archive_part.recording_part_id
);

UPDATE vainglory_publications
SET plan_state='waiting_analysis',needs_refresh=1,force_republish=1,
    state='prepared',attempt_count=0,next_attempt_at=0,
    error='等待失败分析任务重新处理',
    updated_at=CAST(strftime('%s','now') AS INTEGER)
WHERE session_id IN (
    SELECT session_id FROM migration_0062_retry_parts
);

UPDATE vainglory_scan_jobs
SET state='pending',progress=0,error=NULL,
    requested_at=CAST(strftime('%s','now') AS INTEGER),
    started_at=NULL,completed_at=NULL,
    updated_at=CAST(strftime('%s','now') AS INTEGER)
WHERE session_id IN (
    SELECT session_id FROM migration_0062_retry_parts
);

DELETE FROM vainglory_ocr_jobs
WHERE part_id IN (
    SELECT part_id FROM migration_0062_retry_parts
);

UPDATE vainglory_part_jobs
SET state='pending',progress=0,error=NULL,
    requested_at=CAST(strftime('%s','now') AS INTEGER),
    started_at=NULL,completed_at=NULL,
    updated_at=CAST(strftime('%s','now') AS INTEGER)
WHERE part_id IN (
    SELECT part_id FROM migration_0062_retry_parts
);

DROP TABLE migration_0062_retry_parts;
