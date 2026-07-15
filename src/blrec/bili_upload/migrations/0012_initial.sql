CREATE TABLE upload_suppressions (
    session_id INTEGER PRIMARY KEY
        REFERENCES recording_sessions(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    manager_subject TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE upload_job_archives (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL
        REFERENCES recording_sessions(id) ON DELETE CASCADE,
    old_job_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    aid INTEGER,
    bvid TEXT,
    state TEXT NOT NULL,
    submit_state TEXT NOT NULL,
    policy_snapshot_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    archived_at INTEGER NOT NULL
);

CREATE INDEX upload_job_archives_session_idx
ON upload_job_archives(session_id, archived_at, id);
