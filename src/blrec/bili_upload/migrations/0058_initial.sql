ALTER TABLE vainglory_part_jobs
ADD COLUMN candidate_count INTEGER
CHECK (candidate_count IS NULL OR candidate_count >= 0);
