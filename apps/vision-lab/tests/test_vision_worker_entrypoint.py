import pytest
from labeler import vision_worker


@pytest.mark.parametrize(
    'server_url', ['http://192.168.50.24:8800', 'https://example.com/vision']
)
def test_worker_local_control_plane_rejects_remote_task_server(server_url: str) -> None:
    with pytest.raises(RuntimeError, match='本地控制面'):
        vision_worker.validate_local_control_plane_url(server_url, 8801)


@pytest.mark.parametrize(
    'server_url', ['http://127.0.0.1:8801', 'http://localhost:8801']
)
def test_worker_local_control_plane_accepts_matching_loopback_url(
    server_url: str,
) -> None:
    vision_worker.validate_local_control_plane_url(server_url, 8801)


def test_worker_waits_until_local_control_plane_is_ready(monkeypatch) -> None:
    opened = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def open_url(request, timeout):
        opened.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr(vision_worker.urllib.request, 'urlopen', open_url)

    vision_worker.wait_for_local_control_plane(
        'http://127.0.0.1:8801', timeout_seconds=1
    )

    assert opened == [('http://127.0.0.1:8801/api/config', 1)]
