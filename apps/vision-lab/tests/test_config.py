import os

import pytest
from labeler import config


def test_database_url_can_be_loaded_from_private_file(tmp_path, monkeypatch) -> None:
    secret = tmp_path / 'database-url'
    secret.write_text('postgresql://vision:secret@127.0.0.1:15433/blrec\n')
    secret.chmod(0o600)
    monkeypatch.delenv('VISION_LAB_DATABASE_URL', raising=False)
    monkeypatch.setenv('VISION_LAB_DATABASE_URL_FILE', str(secret))

    assert config.read_environment_secret('VISION_LAB_DATABASE_URL') == (
        'postgresql://vision:secret@127.0.0.1:15433/blrec'
    )


def test_database_url_file_rejects_group_or_world_access(tmp_path, monkeypatch) -> None:
    secret = tmp_path / 'database-url'
    secret.write_text('postgresql://vision:secret@127.0.0.1:15433/blrec\n')
    secret.chmod(0o644)
    monkeypatch.delenv('VISION_LAB_DATABASE_URL', raising=False)
    monkeypatch.setenv('VISION_LAB_DATABASE_URL_FILE', str(secret))

    with pytest.raises(RuntimeError, match='权限必须为 600'):
        config.read_environment_secret('VISION_LAB_DATABASE_URL')


def test_environment_secret_keeps_existing_direct_value(monkeypatch) -> None:
    monkeypatch.setenv('VISION_LAB_DATABASE_URL', 'postgresql://direct')
    monkeypatch.delenv('VISION_LAB_DATABASE_URL_FILE', raising=False)

    assert config.read_environment_secret('VISION_LAB_DATABASE_URL') == (
        'postgresql://direct'
    )


def test_worker_token_can_be_loaded_from_private_file(tmp_path, monkeypatch) -> None:
    secret = tmp_path / 'worker-token'
    secret.write_text('private-worker-token\n')
    secret.chmod(0o600)
    monkeypatch.delenv('VISION_LAB_WORKER_TOKEN', raising=False)
    monkeypatch.setenv('VISION_LAB_WORKER_TOKEN_FILE', str(secret))

    assert config.read_environment_secret('VISION_LAB_WORKER_TOKEN') == (
        'private-worker-token'
    )
