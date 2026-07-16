import importlib
from pathlib import Path
from typing import Any


def test_backup_count_is_interpreted_as_days_not_file_count(
    tmp_path: Path, monkeypatch: Any
) -> None:
    module = importlib.import_module('blrec.logging.configure_logging')
    calls = []
    monkeypatch.setattr(module.logger, 'configure', lambda **kwargs: None)
    monkeypatch.setattr(module.logger, 'remove', lambda *args: None)

    def add(*args: object, **kwargs: object) -> int:
        calls.append(kwargs)
        return len(calls)

    monkeypatch.setattr(module.logger, 'add', add)
    monkeypatch.setattr(module, '_console_handler_id', None)
    monkeypatch.setattr(module, '_file_handler_id', None)
    monkeypatch.setattr(module, '_old_log_dir', None)
    monkeypatch.setattr(module, '_old_console_log_level', None)
    monkeypatch.setattr(module, '_old_backup_count', None)

    module.configure_logger(str(tmp_path), backup_count=60)

    file_call = next(call for call in calls if call.get('rotation') == '00:00')
    assert file_call['retention'] == '60 days'
    assert file_call['diagnose'] is False
