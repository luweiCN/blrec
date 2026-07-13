CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

CREATE TABLE bili_accounts (
    id INTEGER PRIMARY KEY,
    uid INTEGER NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    credential_ciphertext BLOB NOT NULL,
    credential_version INTEGER NOT NULL CHECK (credential_version > 0),
    key_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('active','paused','refresh_unknown','archived')
    ),
    pause_reason TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE qr_sessions (
    id TEXT PRIMARY KEY,
    manager_subject TEXT NOT NULL,
    auth_code_hash TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (
        state IN (
            'created','pending','scanned','confirmed','expired','cancelled','failed'
        )
    ),
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE room_upload_policies (
    room_id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES bili_accounts(id),
    enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
    title_template TEXT NOT NULL,
    description_template TEXT NOT NULL,
    tid INTEGER NOT NULL CHECK (tid > 0),
    tags TEXT NOT NULL,
    copyright INTEGER NOT NULL CHECK (copyright IN (1,2)),
    source TEXT NOT NULL,
    auto_comment INTEGER NOT NULL CHECK (auto_comment IN (0,1)),
    danmaku_backfill INTEGER NOT NULL CHECK (danmaku_backfill IN (0,1)),
    filter_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE recording_sessions (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL,
    broadcast_session_key TEXT NOT NULL UNIQUE,
    live_start_time INTEGER,
    state TEXT NOT NULL CHECK (
        state IN ('open','closed','cancelled','manual_review','skipped')
    ),
    started_at INTEGER NOT NULL,
    ended_at INTEGER
);

CREATE TABLE recording_runs (
    id TEXT PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES recording_sessions(id),
    state TEXT NOT NULL CHECK (state IN ('recording','finished','cancelled')),
    started_at INTEGER NOT NULL,
    ended_at INTEGER
);

CREATE TABLE event_journal (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    room_id INTEGER NOT NULL,
    run_id TEXT REFERENCES recording_runs(id),
    path TEXT,
    payload_json TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    consumed_at INTEGER
);

CREATE TABLE upload_jobs (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL UNIQUE REFERENCES recording_sessions(id),
    account_id INTEGER NOT NULL REFERENCES bili_accounts(id),
    policy_snapshot_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'waiting_artifacts','ready','uploading','submitting','waiting_review',
            'approved','rejected','paused','completed'
        )
    ),
    submit_state TEXT NOT NULL CHECK (
        submit_state IN (
            'prepared','in_flight','confirmed','unknown_outcome','failed_permanent'
        )
    ),
    comment_branch_state TEXT NOT NULL DEFAULT 'disabled' CHECK (
        comment_branch_state IN (
            'disabled','pending','running','skipped_no_content',
            'skipped_source_missing','completed','paused','failed'
        )
    ),
    danmaku_branch_state TEXT NOT NULL DEFAULT 'disabled' CHECK (
        danmaku_branch_state IN (
            'disabled','pending','importing','publishing','skipped_source_missing',
            'completed','paused','failed'
        )
    ),
    aid INTEGER,
    bvid TEXT,
    review_reason TEXT,
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_until INTEGER,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE upload_parts (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES upload_jobs(id),
    part_index INTEGER NOT NULL CHECK (part_index > 0),
    source_path TEXT NOT NULL,
    final_path TEXT,
    xml_path TEXT,
    file_identity TEXT,
    artifact_state TEXT NOT NULL CHECK (
        artifact_state IN (
            'recording','postprocessing','ready','failed','missing','manual_review'
        )
    ),
    upload_state TEXT NOT NULL DEFAULT 'prepared' CHECK (
        upload_state IN (
            'prepared','preupload','uploading','completing','confirmed',
            'unknown_outcome','failed'
        )
    ),
    danmaku_import_state TEXT NOT NULL DEFAULT 'disabled' CHECK (
        danmaku_import_state IN (
            'disabled','pending','importing','waiting_capacity','missing_source',
            'completed','failed'
        )
    ),
    remote_filename TEXT,
    cid INTEGER,
    upload_session_json TEXT,
    UNIQUE(job_id, part_index)
);

CREATE TABLE upload_chunks (
    id INTEGER PRIMARY KEY,
    part_id INTEGER NOT NULL REFERENCES upload_parts(id),
    chunk_no INTEGER NOT NULL CHECK (chunk_no >= 0),
    offset INTEGER NOT NULL CHECK (offset >= 0),
    size INTEGER NOT NULL CHECK (size > 0),
    etag TEXT,
    state TEXT NOT NULL CHECK (
        state IN ('prepared','in_flight','confirmed','failed')
    ),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    UNIQUE(part_id, chunk_no)
);

CREATE TABLE comment_items (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES upload_jobs(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    kind TEXT NOT NULL CHECK (kind IN ('root','reply','pin')),
    parent_ordinal INTEGER,
    content TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    rpid INTEGER,
    state TEXT NOT NULL CHECK (
        state IN (
            'prepared','in_flight','confirmed','unknown_outcome','failed_permanent'
        )
    ),
    error_code INTEGER,
    error_message TEXT,
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_until INTEGER,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    UNIQUE(job_id, ordinal)
);

CREATE TABLE danmaku_items (
    id INTEGER PRIMARY KEY,
    part_id INTEGER NOT NULL REFERENCES upload_parts(id),
    xml_identity TEXT NOT NULL,
    original_index INTEGER NOT NULL CHECK (original_index >= 0),
    progress_ms INTEGER NOT NULL CHECK (progress_ms >= 0),
    mode INTEGER NOT NULL,
    fontsize INTEGER NOT NULL,
    color INTEGER NOT NULL,
    content TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    request_fingerprint TEXT NOT NULL,
    dmid INTEGER,
    state TEXT NOT NULL CHECK (
        state IN (
            'prepared','in_flight','confirmed','unknown_outcome','failed_permanent'
        )
    ),
    error_code INTEGER,
    error_message TEXT,
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_until INTEGER,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE(part_id, xml_identity, original_index)
);

CREATE TABLE management_audit (
    id INTEGER PRIMARY KEY,
    manager_subject TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    old_state TEXT,
    new_state TEXT,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX upload_jobs_claim_idx
ON upload_jobs(state, next_attempt_at, priority, id);

CREATE INDEX comment_items_claim_idx
ON comment_items(state, next_attempt_at, priority, id);

CREATE INDEX danmaku_items_claim_idx
ON danmaku_items(state, next_attempt_at, priority, id);
