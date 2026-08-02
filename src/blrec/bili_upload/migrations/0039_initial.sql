CREATE TABLE vainglory_publications (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL
        REFERENCES bili_accounts(id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL
        REFERENCES recording_sessions(id) ON DELETE CASCADE,
    upload_job_id INTEGER
        REFERENCES upload_jobs(id) ON DELETE SET NULL,
    aid INTEGER NOT NULL CHECK (aid > 0),
    bvid TEXT NOT NULL CHECK (
        length(bvid) BETWEEN 10 AND 20
        AND bvid NOT GLOB '*[^0-9A-Za-z]*'
    ),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('upload','archive')),
    payload_hash TEXT NOT NULL CHECK (
        length(payload_hash)=64
        AND payload_hash NOT GLOB '*[^0-9a-f]*'
    ),
    description_block TEXT NOT NULL CHECK (length(description_block) > 0),
    state TEXT NOT NULL CHECK (
        state IN ('prepared','running','confirmed','paused','failed')
    ),
    description_state TEXT NOT NULL CHECK (
        description_state IN (
            'prepared','in_flight','confirmed','skipped_no_room'
        )
    ),
    pin_state TEXT NOT NULL CHECK (
        pin_state IN ('prepared','in_flight','confirmed')
    ),
    root_rpid INTEGER CHECK (root_rpid IS NULL OR root_rpid > 0),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
    UNIQUE(account_id,bvid),
    UNIQUE(upload_job_id)
);

CREATE INDEX vainglory_publications_work_idx
ON vainglory_publications(state,next_attempt_at,id);

CREATE INDEX vainglory_publications_session_idx
ON vainglory_publications(session_id,id);

CREATE TABLE vainglory_publication_comments (
    id INTEGER PRIMARY KEY,
    publication_id INTEGER NOT NULL
        REFERENCES vainglory_publications(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    content TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 1000),
    match_ids_json TEXT NOT NULL,
    uploaded_pictures_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL CHECK (
        state IN (
            'prepared','in_flight','unknown_outcome','confirmed','failed'
        )
    ),
    rpid INTEGER CHECK (rpid IS NULL OR rpid > 0),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
    UNIQUE(publication_id,ordinal)
);

CREATE INDEX vainglory_publication_comments_work_idx
ON vainglory_publication_comments(
    publication_id,state,next_attempt_at,ordinal
);
