CREATE TABLE vainglory_scan_suppressions (
    session_id INTEGER PRIMARY KEY
        REFERENCES recording_sessions(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL CHECK (created_at > 0)
);
