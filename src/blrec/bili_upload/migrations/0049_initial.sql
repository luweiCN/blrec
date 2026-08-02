CREATE TABLE vainglory_players (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK (
        name=trim(name) AND length(name) BETWEEN 1 AND 80
    ),
    origin TEXT NOT NULL CHECK (origin IN ('automatic','manual')),
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at)
);

CREATE INDEX vainglory_players_name_idx
ON vainglory_players(name COLLATE NOCASE,id);

CREATE TABLE vainglory_player_rooms (
    room_id INTEGER PRIMARY KEY CHECK (room_id > 0),
    player_id INTEGER NOT NULL
        REFERENCES vainglory_players(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at)
);

CREATE INDEX vainglory_player_rooms_player_idx
ON vainglory_player_rooms(player_id,room_id);

CREATE TABLE vainglory_player_sessions (
    session_id INTEGER PRIMARY KEY
        REFERENCES recording_sessions(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL
        REFERENCES vainglory_players(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at)
);

CREATE INDEX vainglory_player_sessions_player_idx
ON vainglory_player_sessions(player_id,session_id);

WITH candidates AS (
    SELECT
        session.id AS session_id,
        session.room_id,
        session.anchor_uid,
        trim(session.anchor_name) AS anchor_name,
        session.started_at,
        CASE
            WHEN session.anchor_uid IS NOT NULL AND session.anchor_uid > 0
                THEN 'uid:' || session.anchor_uid
            WHEN session.room_id > 0 THEN 'room:' || session.room_id
            WHEN trim(session.anchor_name) <> ''
                THEN 'name:' || lower(trim(session.anchor_name))
        END AS identity_key
    FROM recording_sessions session
    WHERE EXISTS(
        SELECT 1 FROM vainglory_matches match
        WHERE match.session_id=session.id
    )
), player_groups AS (
    SELECT identity_key,MIN(session_id) AS player_id
    FROM candidates
    WHERE identity_key IS NOT NULL
    GROUP BY identity_key
)
INSERT INTO vainglory_players(id,name,origin,created_at,updated_at)
SELECT
    player_groups.player_id,
    COALESCE(
        (
            SELECT substr(latest.anchor_name,1,80)
            FROM candidates latest
            WHERE latest.identity_key=player_groups.identity_key
              AND latest.anchor_name<>''
            ORDER BY latest.started_at DESC,latest.session_id DESC
            LIMIT 1
        ),
        '玩家 ' || player_groups.player_id
    ),
    'automatic',
    CAST(strftime('%s','now') AS INTEGER),
    CAST(strftime('%s','now') AS INTEGER)
FROM player_groups;

WITH candidates AS (
    SELECT
        session.id AS session_id,
        session.room_id,
        session.anchor_uid,
        session.started_at,
        CASE
            WHEN session.anchor_uid IS NOT NULL AND session.anchor_uid > 0
                THEN 'uid:' || session.anchor_uid
            WHEN session.room_id > 0 THEN 'room:' || session.room_id
            WHEN trim(session.anchor_name) <> ''
                THEN 'name:' || lower(trim(session.anchor_name))
        END AS identity_key
    FROM recording_sessions session
    WHERE EXISTS(
        SELECT 1 FROM vainglory_matches match
        WHERE match.session_id=session.id
    )
), ranked_rooms AS (
    SELECT
        room_id,
        identity_key,
        ROW_NUMBER() OVER (
            PARTITION BY room_id
            ORDER BY started_at DESC,session_id DESC
        ) AS room_rank
    FROM candidates
    WHERE room_id > 0 AND identity_key IS NOT NULL
)
INSERT INTO vainglory_player_rooms(
    room_id,player_id,created_at,updated_at
)
SELECT
    ranked_rooms.room_id,
    (
        SELECT MIN(grouped.session_id)
        FROM candidates grouped
        WHERE grouped.identity_key=ranked_rooms.identity_key
    ),
    CAST(strftime('%s','now') AS INTEGER),
    CAST(strftime('%s','now') AS INTEGER)
FROM ranked_rooms
WHERE ranked_rooms.room_rank=1;

WITH candidates AS (
    SELECT
        session.id AS session_id,
        session.room_id,
        CASE
            WHEN session.anchor_uid IS NOT NULL AND session.anchor_uid > 0
                THEN 'uid:' || session.anchor_uid
            WHEN session.room_id > 0 THEN 'room:' || session.room_id
            WHEN trim(session.anchor_name) <> ''
                THEN 'name:' || lower(trim(session.anchor_name))
        END AS identity_key
    FROM recording_sessions session
    WHERE EXISTS(
        SELECT 1 FROM vainglory_matches match
        WHERE match.session_id=session.id
    )
)
INSERT INTO vainglory_player_sessions(
    session_id,player_id,created_at,updated_at
)
SELECT
    candidate.session_id,
    (
        SELECT MIN(grouped.session_id)
        FROM candidates grouped
        WHERE grouped.identity_key=candidate.identity_key
    ),
    CAST(strftime('%s','now') AS INTEGER),
    CAST(strftime('%s','now') AS INTEGER)
FROM candidates candidate
WHERE candidate.room_id <= 0 AND candidate.identity_key IS NOT NULL;

DELETE FROM vainglory_players
WHERE NOT EXISTS(
        SELECT 1 FROM vainglory_player_rooms room
        WHERE room.player_id=vainglory_players.id
    )
  AND NOT EXISTS(
        SELECT 1 FROM vainglory_player_sessions session_player
        WHERE session_player.player_id=vainglory_players.id
    );
