ALTER TABLE highlight_markers
ADD COLUMN recording_part_id INTEGER REFERENCES recording_parts(id) ON DELETE SET NULL;

ALTER TABLE highlight_markers
ADD COLUMN part_anchor_at_ms INTEGER
CHECK (part_anchor_at_ms IS NULL OR part_anchor_at_ms > 0);

ALTER TABLE highlight_markers
ADD COLUMN current_time_ms INTEGER
CHECK (current_time_ms IS NULL OR current_time_ms >= 0);

ALTER TABLE highlight_markers
ADD COLUMN seekable_end_ms INTEGER
CHECK (seekable_end_ms IS NULL OR seekable_end_ms >= 0);

ALTER TABLE highlight_markers
ADD COLUMN raw_delay_ms INTEGER NOT NULL DEFAULT 0
CHECK (raw_delay_ms BETWEEN 0 AND 86400000);

ALTER TABLE highlight_markers
ADD COLUMN baseline_delay_ms INTEGER NOT NULL DEFAULT 0
CHECK (baseline_delay_ms BETWEEN 0 AND 86400000);

ALTER TABLE highlight_markers
ADD COLUMN effective_rewind_ms INTEGER NOT NULL DEFAULT 0
CHECK (effective_rewind_ms BETWEEN 0 AND 86400000);

CREATE INDEX highlight_markers_recording_part_idx
ON highlight_markers(recording_part_id,id);
