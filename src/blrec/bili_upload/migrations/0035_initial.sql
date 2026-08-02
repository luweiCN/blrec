ALTER TABLE vainglory_match_players
ADD COLUMN last_hits INTEGER
CHECK (last_hits IS NULL OR last_hits BETWEEN 0 AND 999);

ALTER TABLE vainglory_scan_jobs
ADD COLUMN custom_title TEXT
CHECK (
    custom_title IS NULL OR (
        custom_title=trim(custom_title)
        AND length(custom_title) BETWEEN 1 AND 200
    )
);
