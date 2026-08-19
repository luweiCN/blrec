ALTER TABLE vainglory_matches
ADD COLUMN duplicate_review_state TEXT NOT NULL DEFAULT 'none'
CHECK (duplicate_review_state IN ('none','pending','confirmed','dismissed'));

ALTER TABLE vainglory_matches
ADD COLUMN duplicate_review_fingerprint TEXT
CHECK (
    duplicate_review_fingerprint IS NULL OR (
        length(duplicate_review_fingerprint)=67
        AND substr(duplicate_review_fingerprint,1,3)='v1:'
    )
);

CREATE INDEX vainglory_matches_duplicate_review_idx
ON vainglory_matches(duplicate_review_state,id)
WHERE duplicate_review_state IN ('pending','confirmed');
