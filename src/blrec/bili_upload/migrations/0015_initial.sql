ALTER TABLE upload_parts
ADD COLUMN repair_stage TEXT NOT NULL DEFAULT 'none' CHECK (
    repair_stage IN (
        'none','original','original_waiting_review','remux',
        'remux_waiting_review','completed','exhausted'
    )
);

ALTER TABLE upload_parts
ADD COLUMN repair_original_attempts INTEGER NOT NULL DEFAULT 0 CHECK (
    repair_original_attempts BETWEEN 0 AND 1
);

ALTER TABLE upload_parts
ADD COLUMN repair_remux_attempts INTEGER NOT NULL DEFAULT 0 CHECK (
    repair_remux_attempts BETWEEN 0 AND 1
);

ALTER TABLE upload_parts
ADD COLUMN repair_diagnostic TEXT;

ALTER TABLE upload_parts
ADD COLUMN repair_temp_path TEXT;

ALTER TABLE upload_parts
ADD COLUMN repair_original_path TEXT;

ALTER TABLE upload_parts
ADD COLUMN repair_original_identity TEXT;

CREATE INDEX upload_parts_repair_stage_idx
ON upload_parts(repair_stage, job_id, part_index);
