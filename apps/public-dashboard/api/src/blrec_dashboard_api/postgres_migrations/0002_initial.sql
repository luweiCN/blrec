CREATE TABLE dashboard_state (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1),
    snapshot_id TEXT NOT NULL CHECK (snapshot_id ~ '^[A-Za-z0-9-]{1,96}$'),
    content_revision TEXT NOT NULL CHECK (content_revision ~ '^[0-9a-f]{64}$'),
    generated_at TEXT NOT NULL,
    snapshot_json TEXT NOT NULL CHECK (snapshot_json IS JSON),
    updated_at BIGINT NOT NULL
);

CREATE TABLE dashboard_trend_publications (
    publication_date TEXT PRIMARY KEY CHECK (
        publication_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
    ),
    snapshot_id TEXT NOT NULL CHECK (snapshot_id ~ '^[A-Za-z0-9-]{1,96}$'),
    generated_at TEXT NOT NULL,
    source_last_match_id BIGINT NOT NULL CHECK (source_last_match_id >= 0),
    standings_json TEXT NOT NULL CHECK (standings_json IS JSON),
    updated_at BIGINT NOT NULL
);
