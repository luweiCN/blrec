ALTER TABLE vainglory_matches
ADD COLUMN hero_recognition_version INTEGER NOT NULL DEFAULT 1
CHECK (hero_recognition_version > 0);

CREATE INDEX vainglory_matches_hero_rematch_idx
ON vainglory_matches(hero_recognition_version,id)
WHERE result_frame_path IS NOT NULL;
