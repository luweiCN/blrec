ALTER TABLE vainglory_analysis_workers
ADD COLUMN desired_concurrency INTEGER
CHECK (desired_concurrency IS NULL OR desired_concurrency BETWEEN 1 AND 8);
