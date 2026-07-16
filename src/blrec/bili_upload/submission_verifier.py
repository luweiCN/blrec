from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

__all__ = ('SubmissionVerification', 'verify_submission')


@dataclass(frozen=True)
class SubmissionVerification:
    state: str
    checked: Tuple[str, ...]
    missing: Tuple[str, ...]
    mismatches: Tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                'state': self.state,
                'checked': self.checked,
                'missing': self.missing,
                'mismatches': self.mismatches,
            },
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        )


_MISSING = object()


def verify_submission(
    snapshot: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    scheduled_publish_at: Optional[int] = None,
) -> SubmissionVerification:
    data = response.get('data')
    if not isinstance(data, Mapping):
        return SubmissionVerification('failed', (), ('archive',), ())
    archive = data.get('archive')
    if not isinstance(archive, Mapping):
        return SubmissionVerification('failed', (), ('archive',), ())

    expected = _expected_fields(snapshot, scheduled_publish_at)
    actual = _actual_fields(archive, data)
    checked = []
    missing = []
    mismatches = []
    for name, expected_value in expected.items():
        actual_value = actual.get(name, _MISSING)
        if actual_value is _MISSING:
            missing.append(name)
            continue
        checked.append(name)
        if actual_value != expected_value:
            mismatches.append(name)

    state = 'different' if mismatches else 'partial' if missing else 'passed'
    return SubmissionVerification(
        state, tuple(checked), tuple(missing), tuple(mismatches)
    )


def _expected_fields(
    snapshot: Mapping[str, Any], scheduled_publish_at: Optional[int]
) -> Dict[str, Any]:
    normalizers: Dict[str, Callable[[Any], Any]] = {
        'title': _text,
        'description': _text,
        'tid': _integer,
        'tags': _tags,
        'copyright': _integer,
        'is_only_self': _boolean,
        'publish_dynamic': _boolean,
        'no_reprint': _boolean,
        'up_selection_reply': _boolean,
        'up_close_reply': _boolean,
        'up_close_danmu': _boolean,
        'creation_statement_id': _integer,
    }
    expected = {
        name: normalize(snapshot[name])
        for name, normalize in normalizers.items()
        if name in snapshot
    }
    if 'part_titles' in snapshot:
        expected['part_titles'] = tuple(
            _text(value) for value in snapshot.get('part_titles', ())
        )
    if _integer(snapshot.get('copyright')) == 2 and 'source' in snapshot:
        expected['source'] = _text(snapshot.get('source'))
    if scheduled_publish_at is not None:
        expected['scheduled_publish_at'] = int(scheduled_publish_at)
    return expected


def _actual_fields(
    archive: Mapping[str, Any], data: Mapping[str, Any]
) -> Dict[str, Any]:
    actual: Dict[str, Any] = {}
    _copy_alias(actual, archive, 'title', ('title',), _text)
    _copy_alias(actual, archive, 'description', ('desc', 'description'), _text)
    _copy_alias(actual, archive, 'tid', ('tid',), _integer)
    _copy_alias(actual, archive, 'tags', ('tag', 'tags'), _tags)
    _copy_alias(actual, archive, 'copyright', ('copyright',), _integer)
    _copy_alias(actual, archive, 'source', ('source',), _text)
    for field in (
        'is_only_self',
        'no_reprint',
        'up_selection_reply',
        'up_close_reply',
        'up_close_danmu',
    ):
        _copy_alias(actual, archive, field, (field,), _boolean)
    if 'publish_dynamic' in archive:
        actual['publish_dynamic'] = _boolean(archive['publish_dynamic'])
    elif 'no_disturbance' in archive:
        actual['publish_dynamic'] = not _boolean(archive['no_disturbance'])
    if 'creation_statement_id' in archive:
        actual['creation_statement_id'] = _integer(archive['creation_statement_id'])
    else:
        statement = archive.get('creation_statement')
        if isinstance(statement, Mapping) and 'id' in statement:
            actual['creation_statement_id'] = _integer(statement['id'])
    _copy_alias(
        actual,
        archive,
        'scheduled_publish_at',
        ('dtime', 'scheduled_publish_at'),
        _integer,
    )
    videos = data.get('videos')
    if not isinstance(videos, list):
        videos = data.get('Videos')
    if isinstance(videos, list) and all(isinstance(video, Mapping) for video in videos):
        ordered = sorted(
            videos, key=lambda video: _integer(video.get('page') or video.get('index'))
        )
        if all('title' in video or 'part' in video for video in ordered):
            actual['part_titles'] = tuple(
                _text(video.get('title', video.get('part'))) for video in ordered
            )
    return actual


def _copy_alias(
    target: Dict[str, Any],
    source: Mapping[str, Any],
    name: str,
    aliases: Sequence[str],
    normalize: Any,
) -> None:
    for alias in aliases:
        if alias in source:
            target[name] = normalize(source[alias])
            return


def _text(value: Any) -> str:
    return '' if value is None else str(value).strip()


def _integer(value: Any) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.lstrip('-').isdigit():
        return int(value)
    return 0


def _boolean(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() in ('1', 'true')
    return bool(value)


def _tags(value: Any) -> Tuple[str, ...]:
    if isinstance(value, str):
        values = value.replace('，', ',').split(',')
    elif isinstance(value, Sequence):
        values = [str(item) for item in value]
    else:
        values = []
    return tuple(sorted(tag.strip() for tag in values if tag.strip()))
