CREATE INDEX recording_sessions_source_started_idx
ON recording_sessions(source_kind,started_at DESC,id DESC);

CREATE INDEX upload_jobs_state_session_idx
ON upload_jobs(state,session_id);

CREATE INDEX highlight_clips_library_idx
ON highlight_clips(created_at DESC,id DESC)
WHERE state!='cancelled';
