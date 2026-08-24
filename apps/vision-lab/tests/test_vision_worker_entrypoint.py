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


def test_worker_retries_control_plane_failure_without_exiting(
    monkeypatch, tmp_path
) -> None:
    calls = []

    class Client:
        def json(self, method, path, payload=None):
            calls.append((method, path, payload))
            if len(calls) == 1:
                raise TimeoutError('temporary timeout')
            if path.endswith('/claim'):
                return {'job': None}
            return {}

    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(vision_worker.time, 'sleep', sleep)
    worker = vision_worker.VisionWorker(
        client=Client(),
        worker_id='worker-1',
        display_name='Worker 1',
        work_dir=tmp_path / 'work',
        base_models_dir=tmp_path / 'models',
        poll_seconds=1,
        capabilities=['model_prefill'],
    )

    with pytest.raises(KeyboardInterrupt):
        worker.run()

    assert [path for _, path, _ in calls] == [
        '/api/vision-workers/register',
        '/api/vision-workers/register',
        '/api/vision-workers/claim',
    ]
    assert sleeps == [1.0, 1.0]


def test_prefill_failure_does_not_leave_candidate_image_on_worker(
    monkeypatch, tmp_path
) -> None:
    class Client:
        @staticmethod
        def download(_path, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            from PIL import Image

            Image.new('RGB', (64, 36), '#222222').save(destination)

    worker = vision_worker.VisionWorker(
        client=Client(),
        worker_id='worker-1',
        display_name='Worker 1',
        work_dir=tmp_path / 'work',
        base_models_dir=tmp_path / 'models',
        capabilities=['model_prefill'],
    )
    monkeypatch.setattr(worker, '_model_contexts', lambda _models: {})
    monkeypatch.setattr(
        vision_worker.model_prefill,
        'run_core_prefill',
        lambda _path, _contexts: {
            'suggestions': {},
            'errors': {'match_flow': 'broken model'},
        },
    )

    with pytest.raises(RuntimeError, match='match_flow'):
        worker._prefill(
            {'payload': {'frame_id': 17, 'operation': 'core', 'models': {}}}
        )

    assert not (tmp_path / 'work/prefill-cache/17.jpg').exists()


def test_prefill_reports_downloaded_image_dimensions(monkeypatch, tmp_path) -> None:
    class Client:
        @staticmethod
        def download(_path, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            from PIL import Image

            Image.new('RGB', (1280, 720), '#222222').save(destination)

    worker = vision_worker.VisionWorker(
        client=Client(),
        worker_id='worker-1',
        display_name='Worker 1',
        work_dir=tmp_path / 'work',
        base_models_dir=tmp_path / 'models',
        capabilities=['model_prefill'],
    )
    monkeypatch.setattr(worker, '_model_contexts', lambda _models: {})
    monkeypatch.setattr(
        vision_worker.model_prefill,
        'run_core_prefill',
        lambda _path, _contexts: {'suggestions': {}, 'errors': {}},
    )

    result = worker._prefill(
        {'payload': {'frame_id': 18, 'operation': 'core', 'models': {}}}
    )

    assert result['image_width'] == 1280
    assert result['image_height'] == 720


def test_worker_routes_afk_slots_as_one_frame_batch(monkeypatch, tmp_path) -> None:
    class Client:
        @staticmethod
        def download(_path, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            from PIL import Image

            Image.new('RGB', (1280, 720), '#222222').save(destination)

    worker = vision_worker.VisionWorker(
        client=Client(),
        worker_id='worker-1',
        display_name='Worker 1',
        work_dir=tmp_path / 'work',
        base_models_dir=tmp_path / 'models',
        capabilities=['model_prefill'],
    )
    monkeypatch.setattr(worker, '_model_contexts', lambda _models: {'afk_status': {}})
    calls = []

    def run(_path, slots, _contexts, *, screen_type, team_size):
        calls.append((len(slots), screen_type, team_size))
        return {'complete': True, 'slots': slots, 'model_runs': {'afk_status': 'r1'}}

    monkeypatch.setattr(vision_worker.model_prefill, 'run_afk_slots_prefill', run)
    slots = [
        {'side': side, 'slot': slot}
        for side in ('left', 'right')
        for slot in range(1, 4)
    ]
    result = worker._prefill(
        {
            'payload': {
                'frame_id': 19,
                'operation': 'afk_slots',
                'models': {'afk_status': {}},
                'screen_type': 'result_page',
                'team_size': 3,
                'slots': slots,
            }
        }
    )

    assert calls == [(6, 'result_page', 3)]
    assert result['operation'] == 'afk_slots'
