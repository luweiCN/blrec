CREATE INDEX archive_migration_items_recent_idx
ON archive_migration_items(migration_id, published_at DESC, id DESC);
