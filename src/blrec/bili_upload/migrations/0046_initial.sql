ALTER TABLE vainglory_matches
ADD COLUMN recorded_player_source TEXT NOT NULL DEFAULT 'automatic'
CHECK (recorded_player_source IN ('automatic','manual'));
