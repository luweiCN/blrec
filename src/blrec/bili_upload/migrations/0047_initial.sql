ALTER TABLE vainglory_match_players
ADD COLUMN hero_source TEXT NOT NULL DEFAULT 'automatic'
CHECK (hero_source IN ('automatic','manual'));
