CREATE TABLE archive_migration_jobs (
    id INTEGER PRIMARY KEY,
    source_uid INTEGER NOT NULL CHECK (source_uid > 0),
    download_account_id INTEGER NOT NULL
        REFERENCES bili_accounts(id),
    target_account_id INTEGER NOT NULL
        REFERENCES bili_accounts(id),
    state TEXT NOT NULL CHECK (
        state IN ('discovering','running','completed','failed')
    ),
    progress REAL NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
    discovered_count INTEGER NOT NULL DEFAULT 0
        CHECK (discovered_count >= 0),
    completed_count INTEGER NOT NULL DEFAULT 0
        CHECK (completed_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    error TEXT,
    requested_at INTEGER NOT NULL CHECK (requested_at > 0),
    started_at INTEGER,
    completed_at INTEGER,
    updated_at INTEGER NOT NULL CHECK (updated_at >= requested_at),
    UNIQUE(source_uid,target_account_id),
    CHECK (completed_count + failed_count <= discovered_count),
    CHECK (
        (state='failed' AND error IS NOT NULL AND length(error) > 0) OR
        (state!='failed' AND error IS NULL)
    )
);

CREATE INDEX archive_migration_jobs_state_idx
ON archive_migration_jobs(state,requested_at,id);

CREATE TABLE archive_migration_items (
    id INTEGER PRIMARY KEY,
    migration_id INTEGER NOT NULL
        REFERENCES archive_migration_jobs(id) ON DELETE CASCADE,
    aid INTEGER CHECK (aid IS NULL OR aid > 0),
    bvid TEXT NOT NULL CHECK (
        length(bvid) BETWEEN 10 AND 20
        AND bvid NOT GLOB '*[^0-9A-Za-z]*'
    ),
    title TEXT NOT NULL CHECK (
        title=trim(title) AND length(title) BETWEEN 1 AND 200
    ),
    published_at INTEGER,
    state TEXT NOT NULL CHECK (
        state IN (
            'queued','downloading','creating_task','task_created','failed'
        )
    ),
    progress REAL NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
    page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
    downloaded_page_count INTEGER NOT NULL DEFAULT 0
        CHECK (downloaded_page_count >= 0),
    session_id INTEGER UNIQUE
        REFERENCES recording_sessions(id) ON DELETE SET NULL,
    upload_job_id INTEGER UNIQUE
        REFERENCES upload_jobs(id) ON DELETE SET NULL,
    error TEXT,
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
    UNIQUE(migration_id,bvid),
    CHECK (downloaded_page_count <= page_count),
    CHECK (
        (state='failed' AND error IS NOT NULL AND length(error) > 0) OR
        (state!='failed' AND error IS NULL)
    ),
    CHECK (
        state!='task_created' OR (
            session_id IS NOT NULL
            AND upload_job_id IS NOT NULL
            AND progress=1
        )
    )
);

CREATE INDEX archive_migration_items_claim_idx
ON archive_migration_items(state,migration_id,published_at,id);

CREATE INDEX archive_migration_items_migration_idx
ON archive_migration_items(migration_id,created_at,id);
