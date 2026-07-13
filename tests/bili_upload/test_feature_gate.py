import base64
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from blrec.bili_upload import (
    FeatureUnavailable,
    JobState,
    WriteState,
    validate_feature_gate,
)
from blrec.setting import BiliUploadSettings as ExportedBiliUploadSettings
from blrec.setting.models import BiliUploadSettings, EnvSettings, Settings, SettingsIn


def encoded_key(byte: int = 1) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode('ascii')


def write_key_file(path: Path, *, byte: int = 1, mode: int = 0o600) -> None:
    path.write_text(encoded_key(byte), encoding='ascii')
    path.chmod(mode)


def test_bili_upload_settings_have_safe_defaults() -> None:
    settings = BiliUploadSettings()

    assert settings.enabled is False
    assert settings.database_path == '/cfg/blrec.sqlite3'
    assert settings.auto_upload_enabled is False
    assert settings.auto_comment_enabled is False
    assert settings.danmaku_backfill_enabled is False
    assert settings.upload_chunk_size == 4 * 1024 * 1024
    assert settings.upload_chunk_concurrency == 2
    assert settings.danmaku_interval_seconds == 25
    assert settings.import_high_watermark == 1000000


@pytest.mark.parametrize(
    ('field', 'value'),
    (
        ('upload_chunk_size', 1024 * 1024 - 1),
        ('upload_chunk_size', 32 * 1024 * 1024 + 1),
        ('upload_chunk_concurrency', 0),
        ('upload_chunk_concurrency', 4),
        ('danmaku_interval_seconds', 24),
        ('danmaku_interval_seconds', 3601),
        ('import_high_watermark', 9999),
    ),
)
def test_bili_upload_settings_reject_out_of_bounds_values(
    field: str, value: int
) -> None:
    with pytest.raises(ValidationError):
        BiliUploadSettings(**{field: value})


def test_settings_models_include_bili_upload() -> None:
    settings = Settings()
    update = SettingsIn.parse_obj({'biliUpload': {'enabled': True}})

    assert settings.bili_upload == BiliUploadSettings()
    assert update.bili_upload == BiliUploadSettings(enabled=True)
    assert ExportedBiliUploadSettings is BiliUploadSettings


def test_env_settings_read_credential_aliases(monkeypatch, tmp_path: Path) -> None:
    old_key_path = tmp_path / 'old.key'
    write_key_file(old_key_path, byte=2)
    monkeypatch.setenv('BLREC_CREDENTIAL_KEY', encoded_key())
    monkeypatch.setenv('BLREC_CREDENTIAL_OLD_KEY_FILES', f'old={old_key_path}')
    monkeypatch.delenv('BLREC_CREDENTIAL_KEY_FILE', raising=False)

    settings = EnvSettings()

    assert settings.credential_key == encoded_key()
    assert settings.credential_key_file is None
    assert settings.credential_old_key_files == {'old': str(old_key_path)}
    assert settings.load_credential_key() == bytes([1]) * 32
    assert settings.load_old_credential_keys() == {'old': bytes([2]) * 32}


def test_env_settings_reject_both_current_key_sources(tmp_path: Path) -> None:
    key_path = tmp_path / 'current.key'
    write_key_file(key_path)

    with pytest.raises(ValidationError, match='must not both be set'):
        EnvSettings(credential_key=encoded_key(), credential_key_file=str(key_path))


@pytest.mark.parametrize(
    'value',
    ('missing-separator', 'old=relative/path', 'old=/one,old=/two', '= /empty-id'),
)
def test_env_settings_reject_invalid_old_key_mappings(value: str) -> None:
    with pytest.raises(ValidationError):
        EnvSettings(credential_old_key_files=value)


def test_env_settings_reject_old_key_with_current_key_id(tmp_path: Path) -> None:
    current_key = bytes([1]) * 32
    current_key_id = hashlib.sha256(current_key).hexdigest()
    old_key_path = tmp_path / 'old.key'
    write_key_file(old_key_path, byte=2)

    with pytest.raises(ValidationError, match='duplicates current credential key id'):
        EnvSettings(
            credential_key=encoded_key(),
            credential_old_key_files=f'{current_key_id}={old_key_path}',
        )


def test_key_file_loader_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / 'target.key'
    link = tmp_path / 'link.key'
    write_key_file(target)
    link.symlink_to(target)

    with pytest.raises(ValidationError, match='symlink'):
        EnvSettings(credential_key_file=str(link))


def test_key_file_loader_rejects_non_regular_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match='regular file'):
        EnvSettings(credential_key_file=str(tmp_path))


def test_key_file_loader_rejects_group_or_other_permissions(tmp_path: Path) -> None:
    key_path = tmp_path / 'current.key'
    write_key_file(key_path, mode=0o640)

    with pytest.raises(ValidationError, match='0600'):
        EnvSettings(credential_key_file=str(key_path))


@pytest.mark.parametrize('contents', ('not-base64!', encoded_key()[:-4]))
def test_key_sources_must_decode_to_32_bytes(contents: str, tmp_path: Path) -> None:
    key_path = tmp_path / 'current.key'
    key_path.write_text(contents, encoding='ascii')
    key_path.chmod(0o600)

    with pytest.raises(ValidationError, match='32 bytes'):
        EnvSettings(credential_key=contents)
    with pytest.raises(ValidationError, match='32 bytes'):
        EnvSettings(credential_key_file=str(key_path))


def test_write_features_fail_closed_without_both_keys(tmp_path: Path) -> None:
    settings = BiliUploadSettings(
        enabled=True, database_path=str(tmp_path / 'db.sqlite3')
    )

    with pytest.raises(FeatureUnavailable, match='BLREC_API_KEY'):
        validate_feature_gate(settings, api_key=None, credential_key=None)
    with pytest.raises(FeatureUnavailable, match='credential key'):
        validate_feature_gate(settings, api_key='12345678', credential_key=None)


def test_write_features_validate_key_length() -> None:
    settings = BiliUploadSettings(enabled=True)

    with pytest.raises(FeatureUnavailable, match='decode to 32 bytes'):
        validate_feature_gate(settings, api_key='12345678', credential_key=b'short')


def test_disabled_write_features_do_not_require_keys() -> None:
    validate_feature_gate(
        BiliUploadSettings(enabled=False), api_key=None, credential_key=None
    )


def test_enabled_write_features_accept_both_keys() -> None:
    validate_feature_gate(
        BiliUploadSettings(enabled=True), api_key='12345678', credential_key=bytes(32)
    )


def test_upload_state_values_are_stable() -> None:
    assert {state.value for state in WriteState} == {
        'prepared',
        'in_flight',
        'confirmed',
        'unknown_outcome',
        'failed_permanent',
    }
    assert {state.value for state in JobState} == {
        'waiting_artifacts',
        'ready',
        'uploading',
        'submitting',
        'waiting_review',
        'approved',
        'rejected',
        'paused',
        'completed',
    }
