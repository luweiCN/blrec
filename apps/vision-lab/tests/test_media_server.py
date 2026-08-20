from pathlib import Path

from labeler import media_server


def test_nas_media_server_exposes_only_media_and_health_routes() -> None:
    paths = {route.path for route in media_server.app.routes}

    assert '/api/config' in paths
    assert '/api/frames/{frame_id}/image' in paths
    assert '/api/frames/{frame_id}/thumb' in paths
    assert '/api/vision-workers/frames/{frame_id}/image' in paths
    assert '/api/vision-workers/datasets/{version_id}/manifest' in paths
    assert '/api/vision-workers/model-runs/{run_id}/artifact' in paths
    assert '/api/vision-workers/model-runs/{run_id}/metadata' in paths
    assert '/api/vision-workers/model-packages/{package_id}/archive' in paths
    assert '/api/training-candidates/ingest' in paths
    assert '/api/training-review/items/{frame_id}' not in paths
    assert '/api/training-review/items' not in paths
    assert '/api/{path:path}' not in paths


def test_candidate_ingest_forwards_only_the_received_batch(monkeypatch) -> None:
    calls = {}

    class Connection:
        def close(self) -> None:
            calls['closed'] = True

    monkeypatch.setattr(
        media_server.server, '_require_vision_worker', lambda _request: None
    )
    monkeypatch.setattr(media_server.server, '_conn', lambda: Connection())
    monkeypatch.setattr(media_server.server, '_nas', lambda: object())

    def sync(_conn, _nas, items, *, maximum):
        calls['items'] = list(items)
        calls['maximum'] = maximum
        return {'total': len(items), 'inserted': len(items), 'failed': 0}

    monkeypatch.setattr(
        media_server.server.worker_candidates, 'sync_worker_candidates', sync
    )
    item = {'schema_version': 3, 'source_id': 'worker-1'}

    result = media_server.api_training_candidates_ingest(
        {'schema_version': 1, 'candidates': [item]}, object()
    )

    assert result['inserted'] == 1
    assert calls['items'] == [item]
    assert calls['maximum'] == 1
    assert calls['closed'] is True


def test_nas_compose_overrides_the_image_entrypoint() -> None:
    compose = (
        Path(__file__).resolve().parents[1] / 'deploy' / 'nas' / 'compose.yml'
    ).read_text(encoding='utf8')

    assert "entrypoint: ['blrec-vision-media']" in compose
    assert "command: ['blrec-vision-media']" not in compose
