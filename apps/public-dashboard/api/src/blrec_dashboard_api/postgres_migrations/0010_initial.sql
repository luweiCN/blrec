ALTER TABLE ingestion_batches
ADD COLUMN source_revision BIGINT NOT NULL DEFAULT 0
CHECK (source_revision >= 0);

ALTER TABLE ingestion_batches
ADD COLUMN published SMALLINT NOT NULL DEFAULT 1
CHECK (published IN (0,1));

ALTER TABLE players
ADD COLUMN public_visible SMALLINT NOT NULL DEFAULT 1
CHECK (public_visible IN (0,1));

ALTER TABLE matches
ADD COLUMN stats_eligible SMALLINT NOT NULL DEFAULT 1
CHECK (stats_eligible IN (0,1));

ALTER TABLE matches
ADD COLUMN replay_access TEXT NOT NULL DEFAULT 'public'
CHECK (replay_access IN ('public','owner'));

ALTER TABLE matches
ADD COLUMN duplicate_of_match_id BIGINT
REFERENCES matches(source_match_id) ON DELETE SET NULL;

ALTER TABLE matches
ADD COLUMN duplicate_review_state TEXT NOT NULL DEFAULT 'none'
CHECK (duplicate_review_state IN ('none','pending','confirmed','dismissed'));

CREATE INDEX matches_duplicate_of_idx
ON matches(duplicate_of_match_id,source_match_id);

CREATE TABLE dashboard_audience_state (
    audience TEXT PRIMARY KEY CHECK (audience IN ('public','owner')),
    source_revision BIGINT NOT NULL CHECK (source_revision > 0),
    dashboard_payload BYTEA NOT NULL CHECK (
        jsonb_typeof(convert_from(dashboard_payload,'UTF8')::jsonb)='object'
    ),
    live_rooms_payload BYTEA NOT NULL CHECK (
        jsonb_typeof(convert_from(live_rooms_payload,'UTF8')::jsonb)='object'
    ),
    published_at BIGINT NOT NULL CHECK (published_at > 0)
);
