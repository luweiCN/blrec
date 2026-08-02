ALTER TABLE vainglory_matches
ADD COLUMN recorded_player_side TEXT
CHECK (recorded_player_side IS NULL OR recorded_player_side IN ('left','right'));

ALTER TABLE vainglory_matches
ADD COLUMN recorded_player_slot INTEGER
CHECK (recorded_player_slot IS NULL OR recorded_player_slot BETWEEN 1 AND 5);

ALTER TABLE vainglory_matches
ADD COLUMN recorded_player_confidence REAL
CHECK (
    recorded_player_confidence IS NULL
    OR recorded_player_confidence BETWEEN 0 AND 1
);

ALTER TABLE vainglory_matches
ADD COLUMN recorded_player_detection_version INTEGER NOT NULL DEFAULT 0
CHECK (recorded_player_detection_version >= 0);

CREATE INDEX vainglory_matches_recorded_player_backfill_idx
ON vainglory_matches(recorded_player_detection_version,id)
WHERE result_frame_path IS NOT NULL;

UPDATE vainglory_matches
SET recorded_player_detection_version=1
WHERE result_frame_path IS NULL;

UPDATE vainglory_publications
SET needs_refresh=1,updated_at=CAST(strftime('%s','now') AS INTEGER);
