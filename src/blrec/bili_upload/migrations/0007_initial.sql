ALTER TABLE room_upload_policies
ADD COLUMN part_title_template TEXT NOT NULL DEFAULT 'P{{ part_index }}';

ALTER TABLE room_upload_policies
ADD COLUMN dynamic_template TEXT NOT NULL DEFAULT '';

ALTER TABLE room_upload_policies
ADD COLUMN is_only_self INTEGER NOT NULL DEFAULT 0 CHECK (is_only_self IN (0, 1));

ALTER TABLE room_upload_policies
ADD COLUMN publish_dynamic INTEGER NOT NULL DEFAULT 1 CHECK (
    publish_dynamic IN (0, 1)
);

ALTER TABLE room_upload_policies
ADD COLUMN no_reprint INTEGER NOT NULL DEFAULT 1 CHECK (no_reprint IN (0, 1));

ALTER TABLE room_upload_policies
ADD COLUMN up_selection_reply INTEGER NOT NULL DEFAULT 0 CHECK (
    up_selection_reply IN (0, 1)
);

ALTER TABLE room_upload_policies
ADD COLUMN up_close_reply INTEGER NOT NULL DEFAULT 0 CHECK (
    up_close_reply IN (0, 1)
);

ALTER TABLE room_upload_policies
ADD COLUMN up_close_danmu INTEGER NOT NULL DEFAULT 0 CHECK (
    up_close_danmu IN (0, 1)
);

CREATE TABLE upload_category_cache (
    account_id INTEGER PRIMARY KEY REFERENCES bili_accounts(id) ON DELETE CASCADE,
    credential_version INTEGER NOT NULL CHECK (credential_version > 0),
    payload_json TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);
