ALTER TABLE recording_sessions
ADD COLUMN upload_decision TEXT NOT NULL DEFAULT 'follow_room'
CHECK (upload_decision IN ('follow_room','upload','skip'));

ALTER TABLE recording_sessions
ADD COLUMN upload_override_json TEXT;

ALTER TABLE recording_sessions
ADD COLUMN upload_resolution_state TEXT NOT NULL DEFAULT 'pending'
CHECK (upload_resolution_state IN (
    'pending','not_requested','configuration_required','job_created'
));

ALTER TABLE recording_sessions
ADD COLUMN upload_resolution_error TEXT;

ALTER TABLE recording_sessions
ADD COLUMN upload_resolved_at INTEGER;

UPDATE recording_sessions
SET upload_decision=CASE upload_intent
    WHEN 'upload' THEN 'upload'
    WHEN 'skip' THEN 'skip'
    ELSE 'follow_room'
END
WHERE state='open';

UPDATE recording_sessions
SET upload_resolution_state='job_created',
    upload_resolved_at=COALESCE(ended_at,started_at)
WHERE EXISTS (
    SELECT 1 FROM upload_jobs WHERE upload_jobs.session_id=recording_sessions.id
);

UPDATE recording_sessions
SET upload_resolution_state='not_requested',
    upload_resolved_at=COALESCE(ended_at,started_at)
WHERE state!='open'
AND NOT EXISTS (
    SELECT 1 FROM upload_jobs WHERE upload_jobs.session_id=recording_sessions.id
);

CREATE INDEX recording_sessions_upload_resolution_idx
ON recording_sessions(upload_resolution_state,state,live_end_time,id);
