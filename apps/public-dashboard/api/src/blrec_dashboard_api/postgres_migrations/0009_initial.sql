CREATE TABLE dashboard_cache_generations (
    source_revision BIGINT NOT NULL CHECK (source_revision > 0),
    audience TEXT NOT NULL CHECK (audience IN ('public','owner')),
    dashboard_payload BYTEA NOT NULL CHECK (
        jsonb_typeof(convert_from(dashboard_payload,'UTF8')::jsonb)='object'
    ),
    live_rooms_payload BYTEA NOT NULL CHECK (
        jsonb_typeof(convert_from(live_rooms_payload,'UTF8')::jsonb)='object'
    ),
    published_at BIGINT NOT NULL CHECK (published_at > 0),
    PRIMARY KEY (source_revision,audience)
);

CREATE TABLE dashboard_cache_players (
    source_revision BIGINT NOT NULL,
    audience TEXT NOT NULL,
    player_id BIGINT NOT NULL CHECK (player_id > 0),
    player_json TEXT NOT NULL CHECK (jsonb_typeof(player_json::jsonb)='object'),
    PRIMARY KEY (source_revision,audience,player_id),
    FOREIGN KEY (source_revision,audience)
        REFERENCES dashboard_cache_generations(source_revision,audience)
        ON DELETE CASCADE
);

CREATE TABLE dashboard_cache_matches (
    source_revision BIGINT NOT NULL,
    audience TEXT NOT NULL,
    match_id BIGINT NOT NULL CHECK (match_id > 0),
    player_id BIGINT NOT NULL CHECK (player_id > 0),
    season_key TEXT NOT NULL CHECK (
        season_key ~ '^[0-9]{4}-(spring|summer|autumn|winter)$'
    ),
    mode TEXT NOT NULL CHECK (mode IN ('3v3','brawl','5v5')),
    played_at_epoch BIGINT NOT NULL CHECK (played_at_epoch > 0),
    result TEXT NOT NULL CHECK (result IN ('W','L')),
    duration_seconds INTEGER NOT NULL CHECK (
        duration_seconds BETWEEN 1 AND 86400
    ),
    has_replay SMALLINT NOT NULL CHECK (has_replay IN (0,1)),
    match_json TEXT NOT NULL CHECK (jsonb_typeof(match_json::jsonb)='object'),
    ratings_json TEXT NOT NULL CHECK (jsonb_typeof(ratings_json::jsonb)='object'),
    PRIMARY KEY (source_revision,audience,match_id),
    FOREIGN KEY (source_revision,audience,player_id)
        REFERENCES dashboard_cache_players(source_revision,audience,player_id)
        ON DELETE CASCADE
);

CREATE INDEX dashboard_cache_matches_page_idx
ON dashboard_cache_matches(
    source_revision,audience,played_at_epoch DESC,match_id DESC
);

CREATE INDEX dashboard_cache_matches_filter_idx
ON dashboard_cache_matches(
    source_revision,audience,season_key,mode,player_id,
    played_at_epoch DESC,match_id DESC
);

CREATE TABLE dashboard_cache_match_search (
    source_revision BIGINT NOT NULL,
    audience TEXT NOT NULL,
    match_id BIGINT NOT NULL,
    form_index INTEGER NOT NULL CHECK (form_index >= 0),
    normalized TEXT NOT NULL,
    pinyin TEXT NOT NULL,
    initials TEXT NOT NULL,
    PRIMARY KEY (source_revision,audience,match_id,form_index),
    FOREIGN KEY (source_revision,audience,match_id)
        REFERENCES dashboard_cache_matches(source_revision,audience,match_id)
        ON DELETE CASCADE
);

CREATE INDEX dashboard_cache_match_search_normalized_idx
ON dashboard_cache_match_search USING gin(normalized gin_trgm_ops);

CREATE INDEX dashboard_cache_match_search_pinyin_idx
ON dashboard_cache_match_search USING gin(pinyin gin_trgm_ops);

CREATE INDEX dashboard_cache_match_search_initials_idx
ON dashboard_cache_match_search USING gin(initials gin_trgm_ops);

CREATE TABLE dashboard_cache_match_heroes (
    source_revision BIGINT NOT NULL,
    audience TEXT NOT NULL,
    match_id BIGINT NOT NULL,
    hero_name TEXT NOT NULL CHECK (length(hero_name) BETWEEN 1 AND 80),
    PRIMARY KEY (source_revision,audience,match_id,hero_name),
    FOREIGN KEY (source_revision,audience,match_id)
        REFERENCES dashboard_cache_matches(source_revision,audience,match_id)
        ON DELETE CASCADE
);

CREATE INDEX dashboard_cache_match_heroes_filter_idx
ON dashboard_cache_match_heroes(
    source_revision,audience,hero_name,match_id
);

CREATE TABLE dashboard_cache_state (
    audience TEXT PRIMARY KEY CHECK (audience IN ('public','owner')),
    source_revision BIGINT NOT NULL,
    published_at BIGINT NOT NULL CHECK (published_at > 0),
    FOREIGN KEY (source_revision,audience)
        REFERENCES dashboard_cache_generations(source_revision,audience)
);
