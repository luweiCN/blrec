CREATE TABLE operational_notification_states (
    event_code TEXT NOT NULL,
    object_key TEXT NOT NULL,
    healthy INTEGER NOT NULL CHECK (healthy IN (0,1)),
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    observed_at INTEGER NOT NULL CHECK (observed_at > 0),
    PRIMARY KEY(event_code, object_key)
);

CREATE INDEX operational_notification_states_health_idx
ON operational_notification_states(healthy, event_code, observed_at);
