GRANT USAGE ON SCHEMA core TO blrec_dashboard;

GRANT SELECT ON TABLE
    core.schema_migrations,
    core.dashboard_source_state,
    core.recording_sessions,
    core.recording_parts,
    core.upload_parts,
    core.vainglory_players,
    core.vainglory_player_rooms,
    core.vainglory_player_sessions,
    core.vainglory_matches,
    core.vainglory_match_players,
    core.vainglory_heroes,
    core.vainglory_scan_jobs,
    core.vainglory_publications,
    core.vainglory_archive_imports,
    core.vainglory_archive_parts
TO blrec_dashboard;
