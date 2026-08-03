UPDATE vainglory_archive_parts
SET state='ready',
    progress=1,
    error=NULL,
    updated_at=CAST(strftime('%s','now') AS INTEGER)
WHERE state='queued'
  AND EXISTS(
      SELECT 1
      FROM vainglory_part_jobs analysis
      WHERE analysis.part_id=vainglory_archive_parts.recording_part_id
        AND analysis.state='ready'
  );

UPDATE vainglory_scan_jobs
SET state='analyzing',
    progress=COALESCE((
        SELECT MIN(
            0.99,
            1.0*COALESCE(SUM(part.progress),0)/
            CASE WHEN imported.page_count>0 THEN imported.page_count ELSE 1 END
        )
        FROM vainglory_archive_imports imported
        LEFT JOIN vainglory_archive_parts part ON part.import_id=imported.id
        WHERE imported.session_id=vainglory_scan_jobs.session_id
    ),0),
    error=NULL,
    completed_at=NULL,
    updated_at=CAST(strftime('%s','now') AS INTEGER)
WHERE EXISTS(
    SELECT 1
    FROM vainglory_archive_imports imported
    WHERE imported.session_id=vainglory_scan_jobs.session_id
      AND (
          imported.state!='ready'
          OR imported.page_count<=0
          OR imported.completed_page_count!=imported.page_count
          OR (
              SELECT COUNT(*)
              FROM vainglory_archive_parts part
              WHERE part.import_id=imported.id
                AND part.state='ready'
          )!=imported.page_count
      )
);

UPDATE vainglory_publications
SET needs_refresh=1,
    updated_at=CAST(strftime('%s','now') AS INTEGER)
WHERE EXISTS(
    SELECT 1
    FROM vainglory_archive_imports imported
    WHERE imported.session_id=vainglory_publications.session_id
      AND (
          imported.state!='ready'
          OR imported.page_count<=0
          OR imported.completed_page_count!=imported.page_count
          OR (
              SELECT COUNT(*)
              FROM vainglory_archive_parts part
              WHERE part.import_id=imported.id
                AND part.state='ready'
          )!=imported.page_count
      )
);

UPDATE vainglory_publications
SET state='prepared',
    chapter_state='prepared',
    next_attempt_at=0,
    error=NULL,
    updated_at=CAST(strftime('%s','now') AS INTEGER)
WHERE state='confirmed'
  AND chapter_state='confirmed';
