CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

CREATE TABLE ingestion_batches (
    idempotency_key TEXT PRIMARY KEY CHECK (
        length(idempotency_key) BETWEEN 1 AND 128
    ),
    payload_sha256 TEXT NOT NULL CHECK (
        length(payload_sha256)=64
        AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    source_last_match_id INTEGER NOT NULL CHECK (source_last_match_id >= 0),
    match_count INTEGER NOT NULL CHECK (match_count >= 0),
    removed_match_count INTEGER NOT NULL CHECK (removed_match_count >= 0),
    applied_at INTEGER NOT NULL
);

CREATE TABLE players (
    player_id INTEGER PRIMARY KEY CHECK (player_id > 0),
    name TEXT NOT NULL CHECK (
        name=trim(name) AND length(name) BETWEEN 1 AND 80
    ),
    initial TEXT NOT NULL CHECK (
        initial=trim(initial) AND length(initial) BETWEEN 1 AND 4
    ),
    room_label TEXT NOT NULL CHECK (length(room_label) <= 120),
    avatar_url TEXT CHECK (avatar_url IS NULL OR length(avatar_url) <= 2048),
    updated_at INTEGER NOT NULL
);

CREATE TABLE player_rooms (
    player_id INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
    room_id INTEGER NOT NULL CHECK (room_id > 0),
    PRIMARY KEY (player_id,room_id),
    UNIQUE (room_id)
);

CREATE INDEX player_rooms_player_idx ON player_rooms(player_id,room_id);

CREATE TABLE player_aliases (
    player_id INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
    alias TEXT NOT NULL CHECK (
        alias=trim(alias) AND length(alias) BETWEEN 1 AND 80
    ),
    PRIMARY KEY (player_id,alias COLLATE NOCASE)
);

CREATE TABLE matches (
    source_match_id INTEGER PRIMARY KEY CHECK (source_match_id > 0),
    revision_sha256 TEXT NOT NULL CHECK (
        length(revision_sha256)=64
        AND revision_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    season_key TEXT NOT NULL CHECK (
        season_key GLOB '[0-9][0-9][0-9][0-9]-*'
        AND (
            season_key LIKE '%-spring'
            OR season_key LIKE '%-summer'
            OR season_key LIKE '%-autumn'
        )
    ),
    mode TEXT NOT NULL CHECK (mode IN ('3v3','brawl','5v5')),
    played_at TEXT NOT NULL,
    played_at_epoch INTEGER NOT NULL CHECK (played_at_epoch > 0),
    duration_seconds INTEGER NOT NULL CHECK (
        duration_seconds BETWEEN 1 AND 86400
    ),
    result TEXT NOT NULL CHECK (result IN ('W','L')),
    stream_title TEXT NOT NULL CHECK (length(stream_title) <= 240),
    replay_kind TEXT CHECK (replay_kind IN ('match','full')),
    replay_url TEXT CHECK (replay_url IS NULL OR length(replay_url) <= 2048),
    result_image_url TEXT CHECK (
        result_image_url IS NULL OR length(result_image_url) <= 2048
    ),
    result_image_width INTEGER CHECK (result_image_width > 0),
    result_image_height INTEGER CHECK (result_image_height > 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    CHECK ((replay_kind IS NULL)=(replay_url IS NULL)),
    CHECK (
        (result_image_url IS NULL AND result_image_width IS NULL
            AND result_image_height IS NULL)
        OR (result_image_url IS NOT NULL AND result_image_width IS NOT NULL
            AND result_image_height IS NOT NULL)
    )
);

CREATE INDEX matches_player_played_idx
ON matches(player_id,played_at_epoch DESC,source_match_id DESC);

CREATE INDEX matches_season_mode_played_idx
ON matches(season_key,mode,played_at_epoch DESC,source_match_id DESC);

CREATE INDEX matches_mode_played_idx
ON matches(mode,played_at_epoch DESC,source_match_id DESC);

CREATE TABLE match_teams (
    match_id INTEGER NOT NULL
        REFERENCES matches(source_match_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('ally','enemy')),
    side TEXT NOT NULL CHECK (side IN ('left','right')),
    color TEXT NOT NULL CHECK (color IN ('teal','orange')),
    kills INTEGER CHECK (kills >= 0),
    economy INTEGER CHECK (economy >= 0),
    PRIMARY KEY (match_id,role),
    UNIQUE (match_id,side),
    UNIQUE (match_id,color)
);

CREATE TABLE match_participants (
    match_id INTEGER NOT NULL,
    team_role TEXT NOT NULL,
    slot INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 5),
    player_name TEXT NOT NULL CHECK (length(player_name) BETWEEN 1 AND 80),
    hero_name TEXT NOT NULL CHECK (length(hero_name) <= 80),
    kills INTEGER CHECK (kills >= 0),
    deaths INTEGER CHECK (deaths >= 0),
    assists INTEGER CHECK (assists >= 0),
    economy INTEGER CHECK (economy >= 0),
    last_hits INTEGER CHECK (last_hits >= 0),
    is_recorded_player INTEGER NOT NULL CHECK (is_recorded_player IN (0,1)),
    PRIMARY KEY (match_id,team_role,slot),
    FOREIGN KEY (match_id,team_role)
        REFERENCES match_teams(match_id,role) ON DELETE CASCADE
);

CREATE INDEX match_participants_hero_idx
ON match_participants(hero_name COLLATE NOCASE,match_id);

CREATE INDEX match_participants_player_name_idx
ON match_participants(player_name COLLATE NOCASE,match_id);

CREATE TABLE rating_events (
    match_id INTEGER NOT NULL
        REFERENCES matches(source_match_id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    season_key TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('all','3v3','brawl','5v5')),
    match_number INTEGER NOT NULL CHECK (match_number > 0),
    result TEXT NOT NULL CHECK (result IN ('W','L')),
    score_before INTEGER NOT NULL CHECK (score_before BETWEEN 0 AND 3000),
    score_delta INTEGER NOT NULL,
    score_after INTEGER NOT NULL CHECK (score_after BETWEEN 0 AND 3000),
    ability_after REAL NOT NULL CHECK (ability_after > 0 AND ability_after < 1),
    evidence_after REAL NOT NULL CHECK (evidence_after > 0),
    provisional INTEGER NOT NULL CHECK (provisional IN (0,1)),
    model_version INTEGER NOT NULL CHECK (model_version > 0),
    PRIMARY KEY (match_id,scope,season_key),
    CHECK (score_after=score_before+score_delta)
);

CREATE INDEX rating_events_player_scope_season_idx
ON rating_events(
    player_id,scope,season_key,match_number DESC,match_id DESC
);

CREATE VIRTUAL TABLE match_search USING fts5(
    match_id UNINDEXED,
    segment_kind UNINDEXED,
    normalized,
    pinyin,
    initials,
    tokenize='trigram'
);

CREATE TABLE removed_matches (
    source_match_id INTEGER PRIMARY KEY CHECK (source_match_id > 0),
    removed_at INTEGER NOT NULL
);
