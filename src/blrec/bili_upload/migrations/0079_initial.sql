ALTER TABLE vainglory_matches
ADD COLUMN content_fingerprint TEXT
CHECK (
    content_fingerprint IS NULL OR (
        length(content_fingerprint)=67
        AND substr(content_fingerprint,1,3)='v1:'
    )
);

ALTER TABLE vainglory_matches
ADD COLUMN duplicate_of_match_id INTEGER
REFERENCES vainglory_matches(id) ON DELETE SET NULL;

ALTER TABLE vainglory_matches
ADD COLUMN duplicate_checked_at INTEGER
CHECK (duplicate_checked_at IS NULL OR duplicate_checked_at > 0);

CREATE INDEX vainglory_matches_content_fingerprint_idx
ON vainglory_matches(content_fingerprint,id)
WHERE content_fingerprint IS NOT NULL;

CREATE INDEX vainglory_matches_duplicate_of_idx
ON vainglory_matches(duplicate_of_match_id,id)
WHERE duplicate_of_match_id IS NOT NULL;
