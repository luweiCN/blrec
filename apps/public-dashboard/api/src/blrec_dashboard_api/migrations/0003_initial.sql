CREATE TABLE player_live_rooms (
    room_id INTEGER PRIMARY KEY CHECK (room_id > 0),
    player_id INTEGER NOT NULL,
    title TEXT NOT NULL CHECK (length(title) <= 240),
    started_at TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY (player_id,room_id)
        REFERENCES player_rooms(player_id,room_id) ON DELETE CASCADE
);

CREATE INDEX player_live_rooms_player_idx
ON player_live_rooms(player_id,room_id);
