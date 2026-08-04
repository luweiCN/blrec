ALTER TABLE vainglory_matches
ADD COLUMN match_kind TEXT NOT NULL DEFAULT 'unknown'
CHECK (match_kind IN ('pvp','bot','practice','unknown'));

ALTER TABLE vainglory_matches
ADD COLUMN view_context TEXT NOT NULL DEFAULT 'unknown'
CHECK (view_context IN ('played','observed','unknown'));

ALTER TABLE vainglory_matches
ADD COLUMN stats_eligible INTEGER NOT NULL DEFAULT 1
CHECK (stats_eligible IN (0,1));

ALTER TABLE vainglory_matches
ADD COLUMN stats_exclusion_reason TEXT
CHECK (
    (stats_eligible=1 AND stats_exclusion_reason IS NULL)
    OR (
        stats_eligible=0
        AND stats_exclusion_reason IS NOT NULL
        AND stats_exclusion_reason=trim(stats_exclusion_reason)
        AND length(stats_exclusion_reason) BETWEEN 1 AND 64
    )
);

CREATE INDEX vainglory_matches_stats_eligible_idx
ON vainglory_matches(stats_eligible,session_id,id);
