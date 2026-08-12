CREATE TABLE visitor_analytics_events (
    request_id TEXT PRIMARY KEY,
    occurred_at INTEGER NOT NULL,
    event TEXT NOT NULL CHECK(event IN ('pageview','heartbeat')),
    visitor_hash TEXT NOT NULL,
    page TEXT NOT NULL,
    source TEXT NOT NULL,
    device TEXT NOT NULL,
    browser TEXT NOT NULL,
    country TEXT NOT NULL,
    province TEXT NOT NULL,
    city TEXT NOT NULL,
    provider TEXT NOT NULL
) WITHOUT ROWID;

CREATE INDEX visitor_analytics_events_occurred_at
ON visitor_analytics_events(occurred_at);

CREATE INDEX visitor_analytics_events_event_occurred_at
ON visitor_analytics_events(event,occurred_at);

CREATE INDEX visitor_analytics_events_visitor_occurred_at
ON visitor_analytics_events(visitor_hash,occurred_at);

CREATE TABLE visitor_analytics_sync_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    synced_through INTEGER NOT NULL DEFAULT 0,
    initial_sync_complete INTEGER NOT NULL DEFAULT 0
        CHECK(initial_sync_complete IN (0,1)),
    last_started_at INTEGER,
    last_completed_at INTEGER,
    last_error TEXT
);

INSERT INTO visitor_analytics_sync_state(singleton_id) VALUES(1);
