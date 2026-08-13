ALTER TABLE matches ADD COLUMN exact_fingerprint TEXT CHECK (
    exact_fingerprint IS NULL
    OR (
        length(exact_fingerprint)=64
        AND exact_fingerprint NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE INDEX matches_exact_fingerprint_idx
ON matches(exact_fingerprint,player_id,played_at_epoch,source_match_id);
