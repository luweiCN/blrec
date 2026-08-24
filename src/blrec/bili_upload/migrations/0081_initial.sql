ALTER TABLE vainglory_match_players
ADD COLUMN afk_prediction_status TEXT NOT NULL DEFAULT 'unknown'
CHECK (afk_prediction_status IN ('unknown','active','afk'));

ALTER TABLE vainglory_match_players
ADD COLUMN afk_prediction_probability REAL
CHECK (
    afk_prediction_probability IS NULL
    OR afk_prediction_probability BETWEEN 0 AND 1
);

ALTER TABLE vainglory_match_players
ADD COLUMN afk_prediction_model_version TEXT NOT NULL DEFAULT ''
CHECK (length(afk_prediction_model_version) <= 200);

ALTER TABLE vainglory_match_players
ADD COLUMN afk_prediction_gate_reason TEXT NOT NULL DEFAULT ''
CHECK (length(afk_prediction_gate_reason) <= 200);

ALTER TABLE vainglory_match_players
ADD COLUMN afk_manual_override INTEGER
CHECK (afk_manual_override IS NULL OR afk_manual_override IN (0,1));

CREATE INDEX vainglory_match_players_afk_prediction_idx
ON vainglory_match_players(afk_prediction_status,match_id);
