CREATE TABLE dashboard_source_state (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1),
    revision INTEGER NOT NULL CHECK (revision > 0),
    changed_at INTEGER NOT NULL CHECK (changed_at > 0)
);

INSERT INTO dashboard_source_state(singleton_id,revision,changed_at)
VALUES(1,1,CAST(strftime('%s','now') AS INTEGER));

CREATE TRIGGER dashboard_source_vainglory_matches_insert
AFTER INSERT ON vainglory_matches
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_matches_update
AFTER UPDATE ON vainglory_matches
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_matches_delete
AFTER DELETE ON vainglory_matches
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_match_players_insert
AFTER INSERT ON vainglory_match_players
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_match_players_update
AFTER UPDATE ON vainglory_match_players
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_match_players_delete
AFTER DELETE ON vainglory_match_players
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_heroes_insert
AFTER INSERT ON vainglory_heroes
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_heroes_update
AFTER UPDATE ON vainglory_heroes
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_heroes_delete
AFTER DELETE ON vainglory_heroes
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_players_insert
AFTER INSERT ON vainglory_players
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_players_update
AFTER UPDATE ON vainglory_players
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_players_delete
AFTER DELETE ON vainglory_players
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_player_rooms_insert
AFTER INSERT ON vainglory_player_rooms
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_player_rooms_update
AFTER UPDATE ON vainglory_player_rooms
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_player_rooms_delete
AFTER DELETE ON vainglory_player_rooms
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_player_sessions_insert
AFTER INSERT ON vainglory_player_sessions
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_player_sessions_update
AFTER UPDATE ON vainglory_player_sessions
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_player_sessions_delete
AFTER DELETE ON vainglory_player_sessions
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_scan_jobs_insert
AFTER INSERT ON vainglory_scan_jobs
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_scan_jobs_update
AFTER UPDATE OF stats_included ON vainglory_scan_jobs
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_scan_jobs_delete
AFTER DELETE ON vainglory_scan_jobs
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_recording_sessions_insert
AFTER INSERT ON recording_sessions
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_recording_sessions_update
AFTER UPDATE OF room_id,anchor_name,started_at,title ON recording_sessions
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_recording_sessions_delete
AFTER DELETE ON recording_sessions
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_recording_parts_insert
AFTER INSERT ON recording_parts
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_recording_parts_update
AFTER UPDATE OF part_index,record_start_time,record_duration_seconds
ON recording_parts
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_recording_parts_delete
AFTER DELETE ON recording_parts
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_publications_insert
AFTER INSERT ON vainglory_publications
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_publications_update
AFTER UPDATE OF session_id,bvid,source_kind,upload_job_id,public_visible_at
ON vainglory_publications
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_publications_delete
AFTER DELETE ON vainglory_publications
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_archive_imports_insert
AFTER INSERT ON vainglory_archive_imports
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_archive_imports_update
AFTER UPDATE OF account_id,bvid ON vainglory_archive_imports
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_archive_imports_delete
AFTER DELETE ON vainglory_archive_imports
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_archive_parts_insert
AFTER INSERT ON vainglory_archive_parts
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_archive_parts_update
AFTER UPDATE OF recording_part_id,page,duration_seconds ON vainglory_archive_parts
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_vainglory_archive_parts_delete
AFTER DELETE ON vainglory_archive_parts
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_upload_parts_insert
AFTER INSERT ON upload_parts
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_upload_parts_update
AFTER UPDATE OF job_id,part_index,cid ON upload_parts
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;

CREATE TRIGGER dashboard_source_upload_parts_delete
AFTER DELETE ON upload_parts
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;
