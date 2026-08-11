CREATE TABLE dashboard_state (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1),
    snapshot_id TEXT NOT NULL CHECK (
        length(snapshot_id) BETWEEN 1 AND 96
        AND snapshot_id NOT GLOB '*[^A-Za-z0-9-]*'
    ),
    content_revision TEXT NOT NULL CHECK (
        length(content_revision)=64
        AND content_revision NOT GLOB '*[^0-9a-f]*'
    ),
    generated_at TEXT NOT NULL,
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    updated_at INTEGER NOT NULL
);

CREATE TABLE dashboard_trend_publications (
    publication_date TEXT PRIMARY KEY CHECK (
        publication_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
    ),
    snapshot_id TEXT NOT NULL CHECK (
        length(snapshot_id) BETWEEN 1 AND 96
        AND snapshot_id NOT GLOB '*[^A-Za-z0-9-]*'
    ),
    generated_at TEXT NOT NULL,
    source_last_match_id INTEGER NOT NULL CHECK (source_last_match_id >= 0),
    standings_json TEXT NOT NULL CHECK (json_valid(standings_json)),
    updated_at INTEGER NOT NULL
);
