ALTER TABLE matches DROP CONSTRAINT IF EXISTS matches_season_key_check;

ALTER TABLE matches ADD CONSTRAINT matches_season_key_check CHECK (
    season_key ~ '^[0-9]{4}-(spring|summer|autumn|winter)$'
);
