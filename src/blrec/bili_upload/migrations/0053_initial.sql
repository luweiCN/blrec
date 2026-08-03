CREATE TABLE vainglory_publication_stale_comments (
    id INTEGER PRIMARY KEY,
    publication_id INTEGER NOT NULL
        REFERENCES vainglory_publications(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    content TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 1000),
    rpid INTEGER CHECK (rpid IS NULL OR rpid > 0),
    state TEXT NOT NULL CHECK (
        state IN ('prepared','in_flight','unknown_outcome')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at)
);

CREATE INDEX vainglory_publication_stale_comments_work_idx
ON vainglory_publication_stale_comments(
    publication_id,state,next_attempt_at,ordinal
);
