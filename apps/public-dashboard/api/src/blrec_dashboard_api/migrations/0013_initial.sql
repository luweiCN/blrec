ALTER TABLE matches
ADD COLUMN recorded_player_confidence REAL
CHECK (
    recorded_player_confidence IS NULL
    OR recorded_player_confidence BETWEEN 0 AND 1
);

ALTER TABLE matches
ADD COLUMN recorded_player_source TEXT NOT NULL DEFAULT 'automatic'
CHECK (recorded_player_source IN ('automatic','manual'));
