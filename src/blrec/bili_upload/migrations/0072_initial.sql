ALTER TABLE vainglory_publications
ADD COLUMN remote_verified_at INTEGER
CHECK (remote_verified_at IS NULL OR remote_verified_at > 0);

UPDATE vainglory_publications
SET state='prepared',
    remote_verified_at=NULL,
    attempt_count=0,
    next_attempt_at=0,
    error='等待远端复核简介、评论和视频分段',
    priority=0,
    updated_at=CAST(strftime('%s','now') AS INTEGER)
WHERE state='confirmed';
