DROP TRIGGER dashboard_source_recording_sessions_update;

CREATE TRIGGER dashboard_source_recording_sessions_update
AFTER UPDATE OF
    room_id,
    anchor_name,
    started_at,
    title,
    source_kind,
    state,
    ended_at,
    live_start_time,
    live_end_time
ON recording_sessions
BEGIN
    UPDATE dashboard_source_state
    SET revision=revision+1,changed_at=CAST(strftime('%s','now') AS INTEGER)
    WHERE singleton_id=1;
END;
