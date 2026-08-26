ALTER TABLE vainglory_video_sources
ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0
CHECK (attempt_count >= 0);

ALTER TABLE vainglory_video_sources
ADD COLUMN next_attempt_at INTEGER NOT NULL DEFAULT 0
CHECK (next_attempt_at >= 0);

ALTER TABLE vainglory_video_sources
ADD COLUMN last_attempt_error TEXT;

ALTER TABLE vainglory_video_sources
ADD COLUMN last_attempt_interface TEXT;

UPDATE vainglory_video_sources
SET state='pending',
    error=NULL,
    attempt_count=0,
    next_attempt_at=0,
    last_attempt_error=error
WHERE state='failed'
AND error LIKE 'BiliDownloadContractError:%';

CREATE INDEX vainglory_video_sources_retry_idx
ON vainglory_video_sources(state,next_attempt_at,updated_at,part_id);
