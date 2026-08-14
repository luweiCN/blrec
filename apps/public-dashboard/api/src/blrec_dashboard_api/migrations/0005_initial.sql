ALTER TABLE matches
ADD COLUMN analysis_provisional INTEGER NOT NULL DEFAULT 0
CHECK (analysis_provisional IN (0,1));

CREATE INDEX matches_analysis_provisional_idx
ON matches(analysis_provisional,played_at_epoch DESC,source_match_id DESC);
