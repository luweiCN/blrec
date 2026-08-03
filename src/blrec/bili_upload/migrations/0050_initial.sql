ALTER TABLE vainglory_archive_imports
ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0
CHECK (retryable IN (0,1));

ALTER TABLE vainglory_archive_imports
ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0
CHECK (attempt_count >= 0);

ALTER TABLE vainglory_archive_imports
ADD COLUMN next_retry_at INTEGER
CHECK (next_retry_at IS NULL OR next_retry_at > 0);

ALTER TABLE vainglory_archive_syncs
ADD COLUMN retry_after_at INTEGER
CHECK (retry_after_at IS NULL OR retry_after_at > 0);

CREATE INDEX vainglory_archive_imports_retry_idx
ON vainglory_archive_imports(
    state,retryable,next_retry_at,created_at,id
);

UPDATE vainglory_archive_imports
SET retryable=1,
    next_retry_at=CAST(strftime('%s','now') AS INTEGER),
    progress=CASE
        WHEN page_count=0 THEN 0
        ELSE 1.0*completed_page_count/page_count
    END,
    classification_reason='处理失败，等待自动重试'
WHERE state='failed';

UPDATE vainglory_archive_imports
SET state='queued',
    progress=0,
    page_count=0,
    completed_page_count=0,
    error=NULL,
    content_classification='unknown',
    classification_reason=NULL,
    retryable=0,
    next_retry_at=NULL
WHERE state='skipped'
  AND classification_reason IS NULL;

UPDATE vainglory_archive_syncs
SET state='running',
    completed_count=(
        SELECT COUNT(*)
        FROM vainglory_archive_imports imported
        WHERE imported.account_id=vainglory_archive_syncs.account_id
          AND (
              imported.state IN ('ready','skipped')
              OR (imported.state='failed' AND imported.retryable=0)
          )
    ),
    progress=COALESCE((
        SELECT AVG(CASE
            WHEN imported.state IN ('ready','skipped') THEN 1.0
            WHEN imported.state='failed' AND imported.retryable=0 THEN 1.0
            ELSE imported.progress
        END)
        FROM vainglory_archive_imports imported
        WHERE imported.account_id=vainglory_archive_syncs.account_id
    ),1.0),
    error=NULL,
    completed_at=NULL,
    updated_at=CAST(strftime('%s','now') AS INTEGER)
WHERE EXISTS(
    SELECT 1
    FROM vainglory_archive_imports imported
    WHERE imported.account_id=vainglory_archive_syncs.account_id
      AND (
          imported.state IN ('queued','downloading','analyzing')
          OR (imported.state='failed' AND imported.retryable=1)
      )
);

UPDATE vainglory_publications
SET state='prepared',
    chapter_state='prepared',
    next_attempt_at=0,
    error=NULL,
    updated_at=CAST(strftime('%s','now') AS INTEGER)
WHERE chapter_state='skipped';

UPDATE danmaku_items
SET progress_ms=(
    SELECT MAX(0,(recording.record_duration_seconds-1)*1000)
    FROM upload_parts part
    JOIN upload_jobs job ON job.id=part.job_id
    JOIN recording_parts recording
      ON recording.session_id=job.session_id
     AND recording.part_index=part.part_index
    WHERE part.id=danmaku_items.part_id
      AND recording.record_duration_seconds>0
    ORDER BY recording.id DESC
    LIMIT 1
)
WHERE state IN ('prepared','failed_permanent')
  AND EXISTS(
      SELECT 1
      FROM upload_parts part
      JOIN upload_jobs job ON job.id=part.job_id
      JOIN recording_parts recording
        ON recording.session_id=job.session_id
       AND recording.part_index=part.part_index
      WHERE part.id=danmaku_items.part_id
        AND recording.record_duration_seconds>0
        AND danmaku_items.progress_ms>=recording.record_duration_seconds*1000
  );

UPDATE upload_jobs
SET danmaku_branch_state='publishing',
    review_reason=NULL,
    updated_at=CAST(strftime('%s','now') AS INTEGER)
WHERE danmaku_branch_state='failed'
  AND EXISTS(
      SELECT 1
      FROM upload_parts part
      JOIN danmaku_items item ON item.part_id=part.id
      WHERE part.job_id=upload_jobs.id
        AND item.state='failed_permanent'
        AND item.error_code=36714
  )
  AND NOT EXISTS(
      SELECT 1
      FROM upload_parts part
      JOIN danmaku_items item ON item.part_id=part.id
      WHERE part.job_id=upload_jobs.id
        AND item.state='failed_permanent'
        AND item.error_code<>36714
  );

UPDATE danmaku_items
SET state='prepared',
    error_code=NULL,
    error_message='越界时间已调整，等待自动重试',
    lease_owner=NULL,
    lease_until=NULL,
    attempt=0,
    next_attempt_at=CAST(strftime('%s','now') AS INTEGER)
WHERE state='failed_permanent'
  AND error_code=36714;
