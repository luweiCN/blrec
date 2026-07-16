from __future__ import annotations

import json
from typing import Any, Mapping

from loguru import logger

__all__ = ('audit', 'safe_audit_payload')


_SECRET_KEY_FRAGMENTS = (
    'authorization',
    'cookie',
    'credential',
    'csrf',
    'password',
    'secret',
    'token',
    'api_key',
    'apikey',
    'upload_session',
)


def safe_audit_payload(event: str, fields: Mapping[str, Any]) -> str:
    payload = {'event': event, 'result': 'observed'}
    payload.update({key: _redact(key, value) for key, value in fields.items()})
    return json.dumps(
        payload, ensure_ascii=False, separators=(',', ':'), sort_keys=True
    )


def audit(event: str, *, level: str = 'INFO', **fields: Any) -> None:
    logger.log(level, '[audit] {}', safe_audit_payload(event, fields))


def _redact(key: str, value: Any) -> Any:
    normalized = key.lower().replace('-', '_')
    if any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS):
        return '[REDACTED]'
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact(str(child_key), child_value)
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(key, item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)
