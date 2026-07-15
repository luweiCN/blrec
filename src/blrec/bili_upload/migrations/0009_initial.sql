CREATE TABLE cover_assets (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE CHECK (length(sha256) = 64),
    storage_path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL CHECK (length(filename) > 0),
    mime_type TEXT NOT NULL CHECK (mime_type IN ('image/jpeg','image/png')),
    width INTEGER NOT NULL CHECK (width > 0),
    height INTEGER NOT NULL CHECK (height > 0),
    byte_size INTEGER NOT NULL CHECK (byte_size > 0 AND byte_size <= 2097152),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE cover_asset_uploads (
    asset_id INTEGER NOT NULL REFERENCES cover_assets(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES bili_accounts(id) ON DELETE CASCADE,
    remote_url TEXT NOT NULL CHECK (length(remote_url) > 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (asset_id, account_id)
);

CREATE TABLE room_upload_policies_v9 (
    room_id INTEGER PRIMARY KEY,
    account_mode TEXT NOT NULL CHECK (account_mode IN ('primary','fixed')),
    account_id INTEGER REFERENCES bili_accounts(id),
    enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
    title_template TEXT NOT NULL,
    description_template TEXT NOT NULL,
    tid INTEGER NOT NULL CHECK (tid > 0),
    tags TEXT NOT NULL,
    copyright INTEGER NOT NULL CHECK (copyright IN (1,2,3)),
    source TEXT NOT NULL,
    auto_comment INTEGER NOT NULL CHECK (auto_comment IN (0,1)),
    danmaku_backfill INTEGER NOT NULL CHECK (danmaku_backfill IN (0,1)),
    filter_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    part_title_template TEXT NOT NULL,
    dynamic_template TEXT NOT NULL,
    is_only_self INTEGER NOT NULL CHECK (is_only_self IN (0,1)),
    publish_dynamic INTEGER NOT NULL CHECK (publish_dynamic IN (0,1)),
    no_reprint INTEGER NOT NULL CHECK (no_reprint IN (0,1)),
    up_selection_reply INTEGER NOT NULL CHECK (up_selection_reply IN (0,1)),
    up_close_reply INTEGER NOT NULL CHECK (up_close_reply IN (0,1)),
    up_close_danmu INTEGER NOT NULL CHECK (up_close_danmu IN (0,1)),
    creation_statement_id INTEGER NOT NULL,
    original_authorization INTEGER NOT NULL CHECK (
        original_authorization IN (0,1)
    ),
    collection_season_id INTEGER,
    collection_section_id INTEGER,
    cover_mode TEXT NOT NULL DEFAULT 'live' CHECK (
        cover_mode IN ('live','custom')
    ),
    cover_asset_id INTEGER REFERENCES cover_assets(id),
    publish_delay_seconds INTEGER NOT NULL DEFAULT 0 CHECK (
        publish_delay_seconds = 0 OR
        publish_delay_seconds BETWEEN 7200 AND 1296000
    ),
    CHECK (
        (account_mode = 'primary' AND account_id IS NULL) OR
        (account_mode = 'fixed' AND account_id IS NOT NULL)
    ),
    CHECK (
        (creation_statement_id = -2 AND copyright = 2 AND
         original_authorization = 0 AND no_reprint = 0) OR
        (creation_statement_id != -2 AND original_authorization = 1 AND
         copyright = 1 AND no_reprint = 1) OR
        (creation_statement_id != -2 AND original_authorization = 0 AND
         copyright = 3 AND no_reprint = 0)
    ),
    CHECK (
        (collection_season_id IS NULL AND collection_section_id IS NULL) OR
        (collection_season_id > 0 AND collection_section_id > 0)
    ),
    CHECK (
        (cover_mode = 'live' AND cover_asset_id IS NULL) OR
        (cover_mode = 'custom' AND cover_asset_id IS NOT NULL)
    )
);

INSERT INTO room_upload_policies_v9 (
    room_id,account_mode,account_id,enabled,title_template,
    description_template,tid,tags,copyright,source,auto_comment,
    danmaku_backfill,filter_json,created_at,updated_at,part_title_template,
    dynamic_template,is_only_self,publish_dynamic,no_reprint,
    up_selection_reply,up_close_reply,up_close_danmu,creation_statement_id,
    original_authorization
)
SELECT room_id,account_mode,account_id,enabled,title_template,
       description_template,tid,tags,copyright,source,auto_comment,
       danmaku_backfill,filter_json,created_at,updated_at,part_title_template,
       dynamic_template,is_only_self,publish_dynamic,no_reprint,
       up_selection_reply,up_close_reply,up_close_danmu,creation_statement_id,
       original_authorization
FROM room_upload_policies;

DROP TABLE room_upload_policies;

ALTER TABLE room_upload_policies_v9 RENAME TO room_upload_policies;

ALTER TABLE upload_jobs
ADD COLUMN scheduled_publish_at INTEGER CHECK (
    scheduled_publish_at IS NULL OR scheduled_publish_at > 0
);

ALTER TABLE upload_jobs
ADD COLUMN collection_branch_state TEXT NOT NULL DEFAULT 'disabled' CHECK (
    collection_branch_state IN (
        'disabled','pending','running','completed','failed'
    )
);

ALTER TABLE upload_jobs
ADD COLUMN collection_error TEXT;
