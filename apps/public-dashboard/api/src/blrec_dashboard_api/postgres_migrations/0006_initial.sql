CREATE TABLE dashboard_publications (
    publication_date DATE PRIMARY KEY,
    source_revision BIGINT NOT NULL CHECK (source_revision >= 0),
    snapshot_id TEXT NOT NULL CHECK (length(snapshot_id) BETWEEN 1 AND 96),
    generated_at TIMESTAMPTZ NOT NULL,
    source_last_match_id BIGINT NOT NULL CHECK (source_last_match_id >= 0),
    updated_at BIGINT NOT NULL
);

CREATE TABLE dashboard_publication_standings (
    publication_date DATE NOT NULL REFERENCES dashboard_publications(
        publication_date
    ) ON DELETE CASCADE,
    season_key TEXT NOT NULL CHECK (length(season_key) BETWEEN 1 AND 32),
    mode TEXT NOT NULL CHECK (mode IN ('all','3v3','brawl','5v5')),
    player_id BIGINT NOT NULL CHECK (player_id > 0),
    rank INTEGER NOT NULL CHECK (rank > 0),
    rating_score INTEGER NOT NULL CHECK (rating_score BETWEEN 0 AND 1000),
    PRIMARY KEY (publication_date,season_key,mode,player_id),
    UNIQUE (publication_date,season_key,mode,rank)
);

CREATE INDEX dashboard_publication_standings_date_idx
ON dashboard_publication_standings(publication_date,season_key,mode,rank);

CREATE TABLE match_assets (
    source_match_id BIGINT PRIMARY KEY CHECK (source_match_id > 0),
    image_url TEXT NOT NULL CHECK (length(image_url) BETWEEN 1 AND 2048),
    image_width INTEGER NOT NULL CHECK (image_width > 0),
    image_height INTEGER NOT NULL CHECK (image_height > 0),
    image_sha256 TEXT NOT NULL CHECK (image_sha256 ~ '^[0-9a-f]{64}$'),
    updated_at BIGINT NOT NULL
);

CREATE TABLE asset_batches (
    idempotency_key TEXT PRIMARY KEY CHECK (
        length(idempotency_key) BETWEEN 1 AND 128
    ),
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    image_count INTEGER NOT NULL CHECK (image_count >= 0),
    removed_match_count INTEGER NOT NULL CHECK (removed_match_count >= 0),
    applied_at BIGINT NOT NULL
);

INSERT INTO dashboard_publications(
    publication_date,source_revision,snapshot_id,generated_at,
    source_last_match_id,updated_at
)
SELECT publication_date::date,0,snapshot_id,generated_at::timestamptz,
       source_last_match_id,updated_at
FROM dashboard_trend_publications;

INSERT INTO dashboard_publication_standings(
    publication_date,season_key,mode,player_id,rank,rating_score
)
SELECT publication.publication_date::date,season.key,mode.key,
       (standing.value->>'playerId')::bigint,
       (standing.value->>'rank')::integer,
       (standing.value->>'ratingScore')::integer
FROM dashboard_trend_publications publication
CROSS JOIN LATERAL jsonb_each(publication.standings_json::jsonb) season
CROSS JOIN LATERAL jsonb_each(season.value) mode
CROSS JOIN LATERAL jsonb_array_elements(mode.value) standing;

INSERT INTO match_assets(
    source_match_id,image_url,image_width,image_height,image_sha256,updated_at
)
SELECT source_match_id,result_image_url,result_image_width,result_image_height,
       repeat('0',64),updated_at
FROM matches
WHERE result_image_url IS NOT NULL;
