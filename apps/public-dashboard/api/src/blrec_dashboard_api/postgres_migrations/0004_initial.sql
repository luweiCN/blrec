ALTER TABLE matches ADD COLUMN exact_fingerprint TEXT CHECK (
    exact_fingerprint IS NULL OR exact_fingerprint ~ '^[0-9a-f]{64}$'
);

CREATE INDEX matches_exact_fingerprint_idx
ON matches(exact_fingerprint,player_id,played_at_epoch,source_match_id);
