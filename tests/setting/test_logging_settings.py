from pathlib import Path

from blrec.setting.models import LoggingSettings


def test_log_files_are_kept_for_sixty_daily_rotations(tmp_path: Path) -> None:
    settings = LoggingSettings(log_dir=str(tmp_path))

    assert settings.backup_count == 60
