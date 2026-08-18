import io
import urllib.request
from pathlib import Path
from unittest import mock

import pytest
from labeler import config, managed_assets


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()


def test_remote_frame_is_available_without_local_nas_mount(monkeypatch) -> None:
    monkeypatch.setattr(config, 'MEDIA_SERVER_URL', 'http://nas:8800')

    assert managed_assets.frame_available('/data/frames/remote.jpg')


def test_dataset_manifest_is_cached_from_media_server(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, 'WORK_DIR', tmp_path / 'work')
    monkeypatch.setattr(config, 'MEDIA_SERVER_URL', 'http://nas:8800')
    monkeypatch.setattr(config, 'VISION_WORKER_TOKEN', 'worker-secret')
    calls = []

    def open_request(request: urllib.request.Request, timeout: int):
        calls.append(request)
        assert timeout == 300
        assert request.full_url.endswith(
            '/api/vision-workers/datasets/match-flow-v1/manifest'
        )
        assert request.get_header('Authorization') == 'Bearer worker-secret'
        return _Response(b'{"frame_id": 42}\n')

    monkeypatch.setattr(urllib.request, 'urlopen', open_request)
    missing = Path('/data/datasets/match-flow-v1/samples.jsonl')

    first = managed_assets.resolve_dataset_manifest('match-flow-v1', missing)
    second = managed_assets.resolve_dataset_manifest('match-flow-v1', missing)

    assert first == second
    assert first.read_bytes() == b'{"frame_id": 42}\n'
    assert len(calls) == 1


def test_model_run_assets_are_cached_together(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, 'WORK_DIR', tmp_path / 'work')
    monkeypatch.setattr(config, 'MEDIA_SERVER_URL', 'http://nas:8800')
    monkeypatch.setattr(config, 'VISION_WORKER_TOKEN', 'worker-secret')
    responses = {'artifact': b'onnx-model', 'metadata': b'{"kind":"classify"}'}

    def open_request(request: urllib.request.Request, timeout: int):
        return _Response(responses[request.full_url.rsplit('/', 1)[-1]])

    monkeypatch.setattr(urllib.request, 'urlopen', open_request)

    artifact, metadata = managed_assets.resolve_model_run(
        'match-flow-run-1', Path('/data/models/match-flow-run-1/model.onnx')
    )

    assert artifact.read_bytes() == b'onnx-model'
    assert metadata.read_text() == '{"kind":"classify"}'


def test_legacy_model_package_is_cached_from_media_server(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(config, 'WORK_DIR', tmp_path / 'work')
    monkeypatch.setattr(config, 'MEDIA_SERVER_URL', 'http://nas:8800')
    monkeypatch.setattr(config, 'VISION_WORKER_TOKEN', 'worker-secret')

    def open_request(request: urllib.request.Request, timeout: int):
        assert request.full_url.endswith(
            '/api/vision-workers/model-packages/vision-package-1/archive'
        )
        return _Response(b'package-zip')

    monkeypatch.setattr(urllib.request, 'urlopen', open_request)

    archive = managed_assets.resolve_model_package_archive('vision-package-1')

    assert archive.read_bytes() == b'package-zip'


def test_missing_local_asset_without_media_server_stays_missing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(config, 'MEDIA_SERVER_URL', '')

    with pytest.raises(FileNotFoundError):
        managed_assets.resolve_dataset_manifest(
            'match-flow-v1', tmp_path / 'missing.jsonl'
        )
