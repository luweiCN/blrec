CREATE TABLE upload_retry_batches (
    operation_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (
        state IN ('accepted','running','succeeded','failed')
    ),
    total_items INTEGER NOT NULL CHECK (total_items >= 0),
    manager_subject TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE upload_retry_batch_items (
    operation_id TEXT NOT NULL REFERENCES upload_retry_batches(operation_id)
        ON DELETE CASCADE,
    job_id INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued','succeeded','rejected')),
    error_code TEXT,
    PRIMARY KEY(operation_id, job_id)
);

CREATE INDEX upload_retry_batch_items_state_idx
ON upload_retry_batch_items(operation_id, state, job_id);
