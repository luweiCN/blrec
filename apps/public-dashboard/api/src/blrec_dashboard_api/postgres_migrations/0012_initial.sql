ALTER TABLE match_participants
ADD COLUMN afk_status TEXT NOT NULL DEFAULT 'unknown'
CHECK (afk_status IN ('unknown','active','afk'));

ALTER TABLE rating_events
ADD COLUMN afk_adjustment TEXT NOT NULL DEFAULT 'none'
CHECK (afk_adjustment IN (
    'none','protected_loss','undermanned_win','self_afk'
));

ALTER TABLE rating_events
ADD COLUMN afk_player_deficit INTEGER NOT NULL DEFAULT 0
CHECK (afk_player_deficit BETWEEN 0 AND 4);
