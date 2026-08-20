ALTER TABLE ingestion_batches
ADD COLUMN source_revision INTEGER NOT NULL DEFAULT 0
CHECK (source_revision >= 0);

ALTER TABLE ingestion_batches
ADD COLUMN published INTEGER NOT NULL DEFAULT 1
CHECK (published IN (0,1));

ALTER TABLE players
ADD COLUMN public_visible INTEGER NOT NULL DEFAULT 1
CHECK (public_visible IN (0,1));

ALTER TABLE matches
ADD COLUMN stats_eligible INTEGER NOT NULL DEFAULT 1
CHECK (stats_eligible IN (0,1));

ALTER TABLE matches
ADD COLUMN replay_access TEXT NOT NULL DEFAULT 'public'
CHECK (replay_access IN ('public','owner'));

ALTER TABLE matches
ADD COLUMN duplicate_of_match_id INTEGER
REFERENCES matches(source_match_id) ON DELETE SET NULL;

ALTER TABLE matches
ADD COLUMN duplicate_review_state TEXT NOT NULL DEFAULT 'none'
CHECK (duplicate_review_state IN ('none','pending','confirmed','dismissed'));

CREATE INDEX matches_duplicate_of_idx
ON matches(duplicate_of_match_id,source_match_id);

CREATE TABLE dashboard_audience_state (
    audience TEXT PRIMARY KEY CHECK (audience IN ('public','owner')),
    source_revision INTEGER NOT NULL CHECK (source_revision > 0),
    dashboard_payload BLOB NOT NULL CHECK (
        json_valid(CAST(dashboard_payload AS TEXT))
        AND json_type(CAST(dashboard_payload AS TEXT))='object'
    ),
    live_rooms_payload BLOB NOT NULL CHECK (
        json_valid(CAST(live_rooms_payload AS TEXT))
        AND json_type(CAST(live_rooms_payload AS TEXT))='object'
    ),
    published_at INTEGER NOT NULL CHECK (published_at > 0)
);
