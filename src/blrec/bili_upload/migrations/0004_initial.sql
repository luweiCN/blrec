CREATE TABLE room_upload_policies_v4 (
    room_id INTEGER PRIMARY KEY,
    account_mode TEXT NOT NULL CHECK (account_mode IN ('primary','fixed')),
    account_id INTEGER REFERENCES bili_accounts(id),
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
    updated_at INTEGER NOT NULL,
    CHECK (
        (account_mode = 'primary' AND account_id IS NULL) OR
        (account_mode = 'fixed' AND account_id IS NOT NULL)
    )
);

INSERT INTO room_upload_policies_v4 (
    room_id,account_mode,account_id,enabled,title_template,
    description_template,tid,tags,copyright,source,auto_comment,
    danmaku_backfill,filter_json,created_at,updated_at
)
SELECT room_id,'fixed',account_id,enabled,title_template,description_template,
       tid,tags,copyright,source,auto_comment,danmaku_backfill,filter_json,
       created_at,updated_at
FROM room_upload_policies;

DROP TABLE room_upload_policies;

ALTER TABLE room_upload_policies_v4 RENAME TO room_upload_policies;
