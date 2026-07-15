CREATE TABLE room_upload_policies_v8 (
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
    )
);

INSERT INTO room_upload_policies_v8 (
    room_id,account_mode,account_id,enabled,title_template,
    description_template,tid,tags,copyright,source,auto_comment,
    danmaku_backfill,filter_json,created_at,updated_at,part_title_template,
    dynamic_template,is_only_self,publish_dynamic,no_reprint,
    up_selection_reply,up_close_reply,up_close_danmu,creation_statement_id,
    original_authorization
)
SELECT room_id,account_mode,account_id,enabled,title_template,
       description_template,tid,tags,
       CASE
           WHEN copyright = 2 THEN 2
           WHEN no_reprint = 1 THEN 1
           ELSE 3
       END,
       source,auto_comment,danmaku_backfill,filter_json,created_at,updated_at,
       part_title_template,dynamic_template,is_only_self,publish_dynamic,
       CASE WHEN copyright = 1 THEN no_reprint ELSE 0 END,
       up_selection_reply,up_close_reply,up_close_danmu,
       CASE WHEN copyright = 2 THEN -2 ELSE -1 END,
       CASE WHEN copyright = 1 THEN no_reprint ELSE 0 END
FROM room_upload_policies;

DROP TABLE room_upload_policies;

ALTER TABLE room_upload_policies_v8 RENAME TO room_upload_policies;
