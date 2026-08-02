CREATE TABLE vainglory_part_jobs (
    part_id INTEGER PRIMARY KEY
        REFERENCES recording_parts(id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL
        REFERENCES recording_sessions(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (
        state IN ('pending','analyzing','ready','failed')
    ),
    request_kind TEXT NOT NULL DEFAULT 'automatic' CHECK (
        request_kind IN ('automatic','manual','archive')
    ),
    progress REAL NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
    algorithm_version INTEGER NOT NULL CHECK (algorithm_version > 0),
    match_count INTEGER NOT NULL DEFAULT 0 CHECK (match_count >= 0),
    error TEXT,
    requested_at INTEGER NOT NULL CHECK (requested_at > 0),
    started_at INTEGER,
    completed_at INTEGER,
    updated_at INTEGER NOT NULL CHECK (updated_at >= requested_at),
    CHECK (
        (state='failed' AND error IS NOT NULL AND length(error) > 0) OR
        (state!='failed' AND error IS NULL)
    ),
    CHECK (started_at IS NULL OR started_at >= requested_at),
    CHECK (completed_at IS NULL OR completed_at >= requested_at)
);

INSERT INTO vainglory_part_jobs(
    part_id,session_id,state,request_kind,progress,algorithm_version,
    match_count,error,requested_at,started_at,completed_at,updated_at
)
SELECT
    part.id,
    job.session_id,
    CASE job.state WHEN 'analyzing' THEN 'pending' ELSE job.state END,
    'manual',
    CASE job.state
        WHEN 'ready' THEN 1
        WHEN 'analyzing' THEN 0
        ELSE job.progress
    END,
    job.algorithm_version,
    (
        SELECT COUNT(*) FROM vainglory_matches match
        WHERE match.result_part_id=part.id
    ),
    CASE job.state WHEN 'failed' THEN job.error ELSE NULL END,
    job.requested_at,
    CASE job.state WHEN 'analyzing' THEN NULL ELSE job.started_at END,
    CASE job.state WHEN 'analyzing' THEN NULL ELSE job.completed_at END,
    job.updated_at
FROM vainglory_scan_jobs job
JOIN recording_parts part ON part.session_id=job.session_id;

CREATE INDEX vainglory_part_jobs_state_idx
ON vainglory_part_jobs(state,requested_at,part_id);

CREATE INDEX vainglory_part_jobs_session_idx
ON vainglory_part_jobs(session_id,state,part_id);

ALTER TABLE vainglory_matches
ADD COLUMN game_mode TEXT NOT NULL DEFAULT 'unknown'
CHECK (game_mode IN ('3v3','5v5','aram','other','unknown'));

ALTER TABLE vainglory_matches
ADD COLUMN team_size INTEGER
CHECK (team_size IS NULL OR team_size BETWEEN 1 AND 5);

ALTER TABLE vainglory_matches
ADD COLUMN started_at_ms INTEGER NOT NULL DEFAULT 0
CHECK (started_at_ms >= 0);

ALTER TABLE vainglory_matches
ADD COLUMN custom_title TEXT
CHECK (
    custom_title IS NULL OR (
        custom_title=trim(custom_title)
        AND length(custom_title) BETWEEN 1 AND 200
    )
);

UPDATE vainglory_matches
SET team_size=(
    SELECT MAX(player.slot)
    FROM vainglory_match_players player
    WHERE player.match_id=vainglory_matches.id
);

UPDATE vainglory_matches
SET game_mode=CASE team_size
        WHEN 3 THEN '3v3'
        WHEN 5 THEN '5v5'
        ELSE 'unknown'
    END,
    started_at_ms=MAX(
        0,
        result_at_ms-COALESCE(duration_seconds,0)*1000
    );

ALTER TABLE vainglory_match_players
RENAME TO vainglory_match_players_old;

CREATE TABLE vainglory_match_players (
    match_id INTEGER NOT NULL
        REFERENCES vainglory_matches(id) ON DELETE CASCADE,
    side TEXT NOT NULL CHECK (side IN ('left','right')),
    slot INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 5),
    player_name TEXT NOT NULL CHECK (length(player_name) <= 80),
    normalized_name TEXT NOT NULL CHECK (length(normalized_name) <= 80),
    hero_id INTEGER REFERENCES vainglory_heroes(id) ON DELETE SET NULL,
    kills INTEGER CHECK (kills >= 0),
    deaths INTEGER CHECK (deaths >= 0),
    assists INTEGER CHECK (assists >= 0),
    economy INTEGER CHECK (economy >= 0),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    PRIMARY KEY(match_id,side,slot)
);

INSERT INTO vainglory_match_players(
    match_id,side,slot,player_name,normalized_name,hero_id,
    kills,deaths,assists,economy,confidence
)
SELECT
    match_id,side,slot,player_name,normalized_name,hero_id,
    kills,deaths,assists,economy,confidence
FROM vainglory_match_players_old;

DROP TABLE vainglory_match_players_old;

CREATE INDEX vainglory_match_players_name_idx
ON vainglory_match_players(normalized_name,match_id);

CREATE INDEX vainglory_match_players_hero_idx
ON vainglory_match_players(hero_id,match_id);

CREATE TABLE vainglory_archive_syncs (
    account_id INTEGER PRIMARY KEY
        REFERENCES bili_accounts(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (
        state IN ('idle','discovering','running','ready','failed')
    ),
    progress REAL NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
    discovered_count INTEGER NOT NULL DEFAULT 0 CHECK (discovered_count >= 0),
    completed_count INTEGER NOT NULL DEFAULT 0 CHECK (completed_count >= 0),
    error TEXT,
    requested_at INTEGER NOT NULL CHECK (requested_at > 0),
    started_at INTEGER,
    completed_at INTEGER,
    updated_at INTEGER NOT NULL CHECK (updated_at >= requested_at),
    CHECK (completed_count <= discovered_count),
    CHECK (
        (state='failed' AND error IS NOT NULL AND length(error) > 0) OR
        (state!='failed' AND error IS NULL)
    )
);

CREATE TABLE vainglory_archive_imports (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL
        REFERENCES bili_accounts(id) ON DELETE CASCADE,
    aid INTEGER NOT NULL CHECK (aid > 0),
    bvid TEXT NOT NULL CHECK (
        length(bvid) BETWEEN 10 AND 20
        AND bvid NOT GLOB '*[^0-9A-Za-z]*'
    ),
    title TEXT NOT NULL CHECK (
        title=trim(title) AND length(title) BETWEEN 1 AND 200
    ),
    published_at INTEGER,
    session_id INTEGER UNIQUE
        REFERENCES recording_sessions(id) ON DELETE SET NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'queued','downloading','analyzing','ready','failed','skipped'
        )
    ),
    progress REAL NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
    page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
    completed_page_count INTEGER NOT NULL DEFAULT 0
        CHECK (completed_page_count >= 0),
    error TEXT,
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
    UNIQUE(account_id,bvid),
    CHECK (completed_page_count <= page_count),
    CHECK (
        (state='failed' AND error IS NOT NULL AND length(error) > 0) OR
        (state!='failed' AND error IS NULL)
    )
);

CREATE INDEX vainglory_archive_imports_state_idx
ON vainglory_archive_imports(state,created_at,id);

CREATE INDEX vainglory_archive_imports_account_idx
ON vainglory_archive_imports(account_id,published_at DESC,id DESC);

CREATE TABLE vainglory_archive_parts (
    id INTEGER PRIMARY KEY,
    import_id INTEGER NOT NULL
        REFERENCES vainglory_archive_imports(id) ON DELETE CASCADE,
    page INTEGER NOT NULL CHECK (page > 0),
    cid INTEGER NOT NULL CHECK (cid > 0),
    title TEXT NOT NULL CHECK (
        title=trim(title) AND length(title) BETWEEN 1 AND 200
    ),
    duration_seconds INTEGER CHECK (duration_seconds > 0),
    recording_part_id INTEGER UNIQUE
        REFERENCES recording_parts(id) ON DELETE SET NULL,
    state TEXT NOT NULL CHECK (
        state IN ('queued','downloading','analyzing','ready','failed')
    ),
    progress REAL NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
    error TEXT,
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
    UNIQUE(import_id,page),
    UNIQUE(import_id,cid),
    CHECK (
        (state='failed' AND error IS NOT NULL AND length(error) > 0) OR
        (state!='failed' AND error IS NULL)
    )
);

CREATE INDEX vainglory_archive_parts_state_idx
ON vainglory_archive_parts(state,import_id,page);

CREATE TABLE vainglory_video_sources (
    part_id INTEGER PRIMARY KEY
        REFERENCES recording_parts(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL
        REFERENCES bili_accounts(id) ON DELETE CASCADE,
    bvid TEXT NOT NULL CHECK (
        length(bvid) BETWEEN 10 AND 20
        AND bvid NOT GLOB '*[^0-9A-Za-z]*'
    ),
    cid INTEGER NOT NULL CHECK (cid > 0),
    page INTEGER NOT NULL CHECK (page > 0),
    origin TEXT NOT NULL CHECK (origin IN ('upload','archive')),
    state TEXT NOT NULL CHECK (
        state IN ('missing','pending','downloading','ready','failed')
    ),
    retention_kind TEXT NOT NULL DEFAULT 'ten_day' CHECK (
        retention_kind IN ('analysis','ten_day')
    ),
    progress REAL NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
    downloaded_bytes INTEGER NOT NULL DEFAULT 0 CHECK (downloaded_bytes >= 0),
    total_bytes INTEGER CHECK (total_bytes IS NULL OR total_bytes > 0),
    cache_path TEXT,
    original_final_path TEXT,
    original_artifact_state TEXT NOT NULL,
    original_video_deleted_at INTEGER,
    original_file_size_bytes INTEGER CHECK (
        original_file_size_bytes IS NULL OR original_file_size_bytes >= 0
    ),
    cached_at INTEGER,
    expires_at INTEGER,
    error TEXT,
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
    UNIQUE(account_id,bvid,cid),
    CHECK (total_bytes IS NULL OR downloaded_bytes <= total_bytes),
    CHECK (
        (state='failed' AND error IS NOT NULL AND length(error) > 0) OR
        (state!='failed' AND error IS NULL)
    ),
    CHECK (
        state!='ready' OR (
            cache_path IS NOT NULL
            AND cached_at IS NOT NULL
            AND expires_at IS NOT NULL
            AND expires_at > cached_at
        )
    )
);

CREATE INDEX vainglory_video_sources_state_idx
ON vainglory_video_sources(state,updated_at,part_id);

CREATE INDEX vainglory_video_sources_expiry_idx
ON vainglory_video_sources(expires_at,part_id)
WHERE state='ready';
