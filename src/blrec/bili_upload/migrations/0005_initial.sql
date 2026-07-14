CREATE TABLE recording_parts (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES recording_sessions(id),
    run_id TEXT NOT NULL REFERENCES recording_runs(id),
    part_index INTEGER NOT NULL CHECK (part_index > 0),
    source_path TEXT NOT NULL,
    final_path TEXT,
    xml_path TEXT,
    record_start_time INTEGER NOT NULL,
    artifact_state TEXT NOT NULL CHECK (
        artifact_state IN (
            'recording','postprocessing','ready','failed','missing','manual_review'
        )
    ),
    xml_completed INTEGER NOT NULL DEFAULT 0 CHECK (xml_completed IN (0,1)),
    source_completed_at INTEGER,
    postprocessed_at INTEGER,
    error_message TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(session_id, part_index),
    UNIQUE(run_id, source_path)
);

CREATE INDEX recording_parts_run_idx
ON recording_parts(run_id, part_index);

CREATE INDEX recording_sessions_state_idx
ON recording_sessions(state, started_at, id);
