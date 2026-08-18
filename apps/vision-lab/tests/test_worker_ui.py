import pytest
from labeler import config, worker_ui


def test_worker_control_plane_uses_full_local_server_without_api_proxy(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, 'DATABASE_URL', 'postgresql://vision')
    monkeypatch.setattr(config, 'CONTROL_PLANE_ONLY', True)
    monkeypatch.setattr(config, 'MEDIA_SERVER_URL', 'http://nas:8800')

    app = worker_ui.create_worker_control_plane_app()
    paths = [route.path for route in app.routes]
    assert '/api/training-review/items/{frame_id}' in paths
    assert '/api/training-review/items/{frame_id}/hero-lineup' in paths
    assert '/api/training-review/items/{frame_id}/prefill' in paths
    assert '/api/{path:path}' not in paths
    assert any(route.path == '' and route.name == 'static' for route in app.routes)


@pytest.mark.parametrize(
    ('database_url', 'control_plane_only', 'media_server_url', 'message'),
    [
        ('', True, 'http://nas:8800', 'PostgreSQL'),
        ('postgresql://vision', False, 'http://nas:8800', 'CONTROL_PLANE_ONLY'),
        ('postgresql://vision', True, '', 'MEDIA_SERVER_URL'),
    ],
)
def test_worker_control_plane_requires_explicit_safe_configuration(
    monkeypatch,
    database_url: str,
    control_plane_only: bool,
    media_server_url: str,
    message: str,
) -> None:
    monkeypatch.setattr(config, 'DATABASE_URL', database_url)
    monkeypatch.setattr(config, 'CONTROL_PLANE_ONLY', control_plane_only)
    monkeypatch.setattr(config, 'MEDIA_SERVER_URL', media_server_url)

    with pytest.raises(RuntimeError, match=message):
        worker_ui.create_worker_control_plane_app()
