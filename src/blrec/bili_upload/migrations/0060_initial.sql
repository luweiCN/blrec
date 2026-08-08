CREATE TABLE vainglory_match_review_suppressions (
    match_id INTEGER NOT NULL
        REFERENCES vainglory_matches(id) ON DELETE CASCADE,
    review_type TEXT NOT NULL
        CHECK (review_type IN ('hero','recorded_player')),
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    PRIMARY KEY(match_id,review_type)
);
