ALTER TABLE recording_sessions
ADD COLUMN upload_intent TEXT NOT NULL DEFAULT 'none' CHECK (
    upload_intent IN ('none','auto','upload','skip')
);

UPDATE recording_sessions
SET upload_intent='auto'
WHERE EXISTS(
    SELECT 1 FROM upload_jobs WHERE upload_jobs.session_id=recording_sessions.id
);

UPDATE recording_sessions
SET upload_intent='skip'
WHERE EXISTS(
    SELECT 1 FROM upload_suppressions
    WHERE upload_suppressions.session_id=recording_sessions.id
);

CREATE INDEX recording_sessions_upload_intent_idx
ON recording_sessions(upload_intent, state, started_at);
