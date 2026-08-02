ALTER TABLE vainglory_publications
ADD COLUMN chapter_state TEXT NOT NULL DEFAULT 'prepared'
CHECK (chapter_state IN ('prepared','confirmed','skipped'));

UPDATE vainglory_publications
SET state='prepared',chapter_state='prepared',next_attempt_at=0,error=NULL,
    updated_at=CAST(strftime('%s','now') AS INTEGER)
WHERE state='confirmed';
