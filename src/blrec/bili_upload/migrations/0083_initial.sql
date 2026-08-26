ALTER TABLE vainglory_match_players
ADD COLUMN hero_prediction_probability REAL
CHECK (
    hero_prediction_probability IS NULL
    OR (hero_prediction_probability >= 0 AND hero_prediction_probability <= 1)
);
