CREATE TABLE vainglory_analysis_workers (
    worker_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    model_package_id TEXT NOT NULL DEFAULT '',
    pipeline_version TEXT NOT NULL DEFAULT '',
    concurrency INTEGER NOT NULL DEFAULT 0 CHECK(concurrency>=0),
    first_seen_at INTEGER,
    last_seen_at INTEGER,
    completed_task_count INTEGER NOT NULL DEFAULT 0 CHECK(completed_task_count>=0),
    failed_task_count INTEGER NOT NULL DEFAULT 0 CHECK(failed_task_count>=0),
    total_processing_seconds REAL NOT NULL DEFAULT 0
        CHECK(total_processing_seconds>=0),
    profiled_task_count INTEGER NOT NULL DEFAULT 0
        CHECK(profiled_task_count>=0),
    profiled_video_seconds REAL NOT NULL DEFAULT 0
        CHECK(profiled_video_seconds>=0),
    total_decode_analysis_seconds REAL NOT NULL DEFAULT 0
        CHECK(total_decode_analysis_seconds>=0),
    total_profiled_task_seconds REAL NOT NULL DEFAULT 0
        CHECK(total_profiled_task_seconds>=0),
    last_task_finished_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX vainglory_analysis_workers_last_seen
ON vainglory_analysis_workers(last_seen_at DESC,worker_id);
