import json

from labeler import config, db, server


def test_service_runtime_state_is_upserted_by_service_key(tmp_path) -> None:
    conn = db.connect(tmp_path / 'lab.db')
    try:
        db.save_service_runtime_state(
            conn, 'candidate_index', {'running': True, 'processed': 12}
        )
        db.save_service_runtime_state(
            conn, 'candidate_index', {'running': False, 'processed': 20}
        )

        state = db.load_service_runtime_state(conn, 'candidate_index')
        rows = conn.execute(
            'SELECT service_key, state_json FROM service_runtime_states'
        ).fetchall()
    finally:
        conn.close()

    assert state == {'running': False, 'processed': 20}
    assert len(rows) == 1
    assert rows[0]['service_key'] == 'candidate_index'
    assert json.loads(rows[0]['state_json']) == state


def test_unknown_service_runtime_state_is_empty(tmp_path) -> None:
    conn = db.connect(tmp_path / 'lab.db')
    try:
        assert db.load_service_runtime_state(conn, 'missing') == {}
    finally:
        conn.close()


def test_candidate_index_process_publishes_state_for_worker(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / 'lab.db'
    conn = db.connect(database)
    conn.close()
    monkeypatch.setattr(config, 'CONTROL_PLANE_ONLY', True)
    monkeypatch.setattr(config, 'CANDIDATE_LOCAL_DIR', tmp_path / 'candidates')
    monkeypatch.setattr(server, '_conn', lambda: db.connect(database))
    monkeypatch.setattr(server, '_worker_candidate_state_last_persisted_at', 0.0)

    server._set_worker_candidate_sync_state(
        running=False,
        processed=73,
        last_completed_at='2026-08-18T10:00:00',
        force_persist=True,
    )

    check = db.connect(database)
    try:
        state = db.load_service_runtime_state(check, 'candidate_index')
    finally:
        check.close()
    assert state['processed'] == 73
    assert state['last_completed_at'] == '2026-08-18T10:00:00'


def test_worker_candidate_state_reads_nas_published_progress(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / 'lab.db'
    conn = db.connect(database)
    try:
        db.save_service_runtime_state(
            conn, 'candidate_index', {'running': True, 'processed': 91, 'total': 200}
        )
    finally:
        conn.close()
    monkeypatch.setattr(config, 'CANDIDATE_LOCAL_DIR', None)
    monkeypatch.setattr(server, '_conn', lambda: db.connect(database))
    monkeypatch.setitem(server._training_review_cache, 'stats', None)

    response = server.api_worker_candidate_state()

    assert response['sync']['running'] is True
    assert response['sync']['processed'] == 91
