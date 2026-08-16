CREATE TABLE vainglory_player_aliases (
    alias TEXT PRIMARY KEY COLLATE NOCASE CHECK (
        alias=trim(alias) AND length(alias) BETWEEN 1 AND 80
    ),
    player_id INTEGER NOT NULL
        REFERENCES vainglory_players(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at)
);

CREATE INDEX vainglory_player_aliases_player_idx
ON vainglory_player_aliases(player_id,alias);

INSERT OR IGNORE INTO vainglory_player_aliases(
    alias,player_id,created_at,updated_at
)
SELECT name,id,created_at,updated_at
FROM vainglory_players;

ALTER TABLE vainglory_archive_syncs
ADD COLUMN daily_limit_override INTEGER
CHECK (daily_limit_override BETWEEN 1 AND 1000);

ALTER TABLE archive_migration_jobs
ADD COLUMN daily_limit_override INTEGER
CHECK (daily_limit_override BETWEEN 1 AND 1000);

ALTER TABLE vainglory_archive_imports
ADD COLUMN is_only_self INTEGER
CHECK (is_only_self IS NULL OR is_only_self IN (0,1));

ALTER TABLE vainglory_archive_imports
ADD COLUMN anchor_identity_checked_at INTEGER
CHECK (
    anchor_identity_checked_at IS NULL OR anchor_identity_checked_at > 0
);

ALTER TABLE vainglory_archive_imports
ADD COLUMN anchor_identity_error TEXT
CHECK (
    anchor_identity_error IS NULL OR length(anchor_identity_error) BETWEEN 1 AND 500
);

CREATE INDEX vainglory_archive_imports_anchor_identity_idx
ON vainglory_archive_imports(anchor_identity_checked_at,id);

ALTER TABLE vainglory_publications
ADD COLUMN visibility_scope TEXT NOT NULL DEFAULT 'unknown'
CHECK (visibility_scope IN ('unknown','public','owner'));

ALTER TABLE vainglory_publications
ADD COLUMN visibility_verified_at INTEGER
CHECK (visibility_verified_at IS NULL OR visibility_verified_at > 0);

UPDATE vainglory_publications
SET visibility_scope='public',
    visibility_verified_at=public_visible_at
WHERE public_visible_at IS NOT NULL;
