CREATE TABLE media_library_items (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL UNIQUE
        REFERENCES recording_sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('broadcast','clip')),
    origin TEXT NOT NULL CHECK (origin IN ('recording','upload')),
    storage_key TEXT NOT NULL UNIQUE CHECK (
        length(storage_key)=32 AND storage_key NOT GLOB '*[^0-9a-f]*'
    ),
    display_name TEXT NOT NULL CHECK (
        display_name=trim(display_name) AND length(display_name) BETWEEN 1 AND 200
    ),
    note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 2000),
    state TEXT NOT NULL CHECK (
        state IN ('uploading','moving','ready','failed')
    ),
    error TEXT,
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at > 0),
    CHECK (updated_at >= created_at),
    CHECK (
        (state='failed' AND error IS NOT NULL AND length(error) > 0) OR
        (state!='failed' AND error IS NULL)
    )
);

CREATE INDEX media_library_items_list_idx
ON media_library_items(kind,created_at DESC,id DESC);

CREATE TABLE media_library_tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK (
        name=trim(name) AND length(name) BETWEEN 1 AND 40
    )
);

CREATE TABLE media_library_item_tags (
    item_id INTEGER NOT NULL
        REFERENCES media_library_items(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL
        REFERENCES media_library_tags(id) ON DELETE CASCADE,
    PRIMARY KEY(item_id,tag_id)
);

CREATE INDEX media_library_item_tags_tag_idx
ON media_library_item_tags(tag_id,item_id);

CREATE TABLE media_library_parts (
    item_id INTEGER NOT NULL
        REFERENCES media_library_items(id) ON DELETE CASCADE,
    part_index INTEGER NOT NULL CHECK (part_index > 0),
    recording_part_id INTEGER
        REFERENCES recording_parts(id) ON DELETE CASCADE,
    original_filename TEXT NOT NULL CHECK (
        length(original_filename) BETWEEN 1 AND 512
    ),
    storage_path TEXT NOT NULL UNIQUE CHECK (length(storage_path) > 0),
    staging_path TEXT UNIQUE,
    expected_size INTEGER NOT NULL CHECK (expected_size > 0),
    received_size INTEGER NOT NULL DEFAULT 0 CHECK (
        received_size BETWEEN 0 AND expected_size
    ),
    state TEXT NOT NULL CHECK (
        state IN ('pending','uploading','uploaded','ready','failed')
    ),
    error TEXT,
    PRIMARY KEY(item_id,part_index),
    CHECK (
        (state='failed' AND error IS NOT NULL AND length(error) > 0) OR
        (state!='failed' AND error IS NULL)
    ),
    CHECK (
        state!='ready' OR
        (recording_part_id IS NOT NULL AND received_size=expected_size)
    ),
    CHECK (state!='uploaded' OR received_size=expected_size)
);

CREATE UNIQUE INDEX media_library_parts_recording_part_idx
ON media_library_parts(recording_part_id)
WHERE recording_part_id IS NOT NULL;

CREATE INDEX media_library_parts_upload_idx
ON media_library_parts(item_id,state,part_index);

CREATE TABLE media_library_file_moves (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL
        REFERENCES media_library_items(id) ON DELETE CASCADE,
    source_path TEXT NOT NULL CHECK (length(source_path) > 0),
    target_path TEXT NOT NULL UNIQUE CHECK (length(target_path) > 0),
    state TEXT NOT NULL DEFAULT 'pending' CHECK (
        state IN ('pending','ready','failed')
    ),
    error TEXT,
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at > 0),
    UNIQUE(item_id,source_path),
    CHECK (updated_at >= created_at),
    CHECK (
        (state='failed' AND error IS NOT NULL AND length(error) > 0) OR
        (state!='failed' AND error IS NULL)
    )
);

CREATE INDEX media_library_file_moves_state_idx
ON media_library_file_moves(state,item_id,id);

CREATE INDEX highlight_clips_source_library_idx
ON highlight_clips(
    CASE WHEN source_session_id IS NULL
        THEN 'clip:'||id ELSE 'session:'||source_session_id END,
    created_at DESC,
    id DESC
)
WHERE state!='cancelled' AND deletion_state IN ('none','failed');
