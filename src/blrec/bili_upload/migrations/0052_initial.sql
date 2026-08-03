CREATE TABLE vainglory_player_room_suppressions (
    room_id INTEGER PRIMARY KEY CHECK (room_id > 0),
    created_at INTEGER NOT NULL CHECK (created_at > 0)
);
