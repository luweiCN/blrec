from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from lxml import etree

from .database import BiliUploadDatabase

__all__ = ('DanmakuFilter', 'DanmakuImporter', 'ImportedDanmaku')


@dataclass(frozen=True)
class DanmakuFilter:
    blocked_phrases: Tuple[str, ...] = ()
    minimum_user_level: Optional[int] = None
    minimum_fan_medal_level: Optional[int] = None

    def __post_init__(self) -> None:
        if self.minimum_user_level is not None and self.minimum_user_level < 0:
            raise ValueError('minimum user level must not be negative')
        if (
            self.minimum_fan_medal_level is not None
            and self.minimum_fan_medal_level < 0
        ):
            raise ValueError('minimum fan medal level must not be negative')

    @classmethod
    def from_policy(cls, value: Any) -> 'DanmakuFilter':
        if not isinstance(value, Mapping):
            return cls()
        blocked_value = value.get('blockedWords')
        blocked: Tuple[str, ...] = ()
        if isinstance(blocked_value, list):
            blocked = tuple(
                item.strip()
                for item in blocked_value
                if isinstance(item, str) and item.strip()
            )
        return cls(
            blocked_phrases=blocked,
            minimum_user_level=cls._threshold(value.get('minimumUserLevel')),
            minimum_fan_medal_level=cls._threshold(value.get('minimumFanMedalLevel')),
        )

    def allows(self, row: 'ImportedDanmaku') -> bool:
        if row.is_lottery is True or row.is_system is True:
            return False
        folded = row.content.casefold()
        if any(phrase.casefold() in folded for phrase in self.blocked_phrases):
            return False
        if (
            self.minimum_user_level is not None
            and row.user_level is not None
            and row.user_level < self.minimum_user_level
        ):
            return False
        if (
            self.minimum_fan_medal_level is not None
            and row.fan_medal_level is not None
            and row.fan_medal_level < self.minimum_fan_medal_level
        ):
            return False
        return True

    @staticmethod
    def _threshold(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 0:
            return value
        return None


@dataclass(frozen=True)
class ImportedDanmaku:
    original_index: int
    progress_ms: int
    mode: int
    fontsize: int
    color: int
    content: str
    priority: int
    source_event_id: Optional[str] = None
    is_system: Optional[bool] = None
    is_lottery: Optional[bool] = None
    user_level: Optional[int] = None
    fan_medal_level: Optional[int] = None


class DanmakuImporter:
    _IDENTITY_BLOCK_SIZE = 64 * 1024
    _ACTIVE_STATES = ('prepared', 'in_flight', 'unknown_outcome')

    def __init__(
        self,
        database: BiliUploadDatabase,
        *,
        insert_batch_size: int = 500,
        import_high_watermark: int = 1_000_000,
        space_threshold_bytes: int = 1024**3,
        free_space: Optional[Callable[[Path], int]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if insert_batch_size <= 0:
            raise ValueError('insert batch size must be positive')
        if import_high_watermark <= 0:
            raise ValueError('import high watermark must be positive')
        if space_threshold_bytes < 0:
            raise ValueError('space threshold must not be negative')
        self._database = database
        self._insert_batch_size = insert_batch_size
        self._import_high_watermark = import_high_watermark
        self._space_threshold_bytes = space_threshold_bytes
        self._free_space = free_space or self._disk_free
        self._clock = clock

    async def create(self, job_id: int) -> None:
        job = await self._database.fetchone(
            'SELECT job.state,job.danmaku_branch_state,session.deletion_state '
            'FROM upload_jobs job JOIN recording_sessions session '
            'ON session.id=job.session_id WHERE job.id=?',
            (job_id,),
        )
        if job is None:
            raise ValueError("unknown upload job '{}'".format(job_id))
        if str(job['danmaku_branch_state']) != 'pending':
            return
        if str(job['deletion_state']) != 'none':
            return
        if str(job['state']) != 'approved':
            raise ValueError('danmaku job is not ready')
        parts = await self._database.fetchall(
            'SELECT id,xml_path,cid FROM upload_parts '
            'WHERE job_id=? ORDER BY part_index',
            (job_id,),
        )
        if not parts:
            await self._mark_job_missing(job_id, ())
            return
        missing_ids = [
            int(row['id'])
            for row in parts
            if row['xml_path'] is None or not os.path.isfile(str(row['xml_path']))
        ]
        if missing_ids:
            placeholders = ','.join('?' for _ in missing_ids)
            await self._database.execute(
                "UPDATE upload_parts SET danmaku_import_state='missing_source' "
                'WHERE job_id=? AND id IN ({})'.format(placeholders),
                (job_id, *missing_ids),
            )
        available_parts = [row for row in parts if int(row['id']) not in missing_ids]
        if any(self._positive_int(row['cid']) is None for row in available_parts):
            raise ValueError('danmaku job has a part without CID')
        updated = await self._database.execute(
            "UPDATE upload_jobs SET danmaku_branch_state='importing',updated_at=? "
            "WHERE id=? AND state='approved' AND danmaku_branch_state='pending'",
            (int(self._clock()), job_id),
        )
        if updated != 1:
            return
        for row in available_parts:
            await self.import_part(int(row['id']), str(row['xml_path']))
            state = await self._database.scalar(
                'SELECT danmaku_import_state FROM upload_parts WHERE id=?',
                (int(row['id']),),
            )
            if state == 'waiting_capacity':
                break
        await self._refresh_job_state(job_id)

    async def run_once(self) -> Optional[int]:
        row = await self._database.fetchone(
            'SELECT part.id,part.xml_path,part.danmaku_import_state '
            'FROM upload_parts part JOIN upload_jobs job ON job.id=part.job_id '
            'JOIN recording_sessions session ON session.id=job.session_id '
            "WHERE job.state='approved' "
            "AND session.deletion_state='none' "
            "AND job.danmaku_branch_state='importing' "
            "AND part.danmaku_import_state IN "
            "('pending','importing','waiting_capacity') "
            'ORDER BY part.job_id,part.part_index LIMIT 1'
        )
        if row is None:
            return None
        part_id = int(row['id'])
        path = Path(str(row['xml_path'] or ''))
        if path.is_file() and (
            await self._active_count() >= self._import_high_watermark
            or not self._has_disk_capacity(path)
        ):
            await self._database.execute(
                "UPDATE upload_parts SET danmaku_import_state='waiting_capacity' "
                'WHERE id=?',
                (part_id,),
            )
            return None
        await self.import_part(part_id, str(path))
        return part_id

    async def import_part(
        self,
        part_id: int,
        xml_path: Optional[str] = None,
        danmaku_filter: Optional[DanmakuFilter] = None,
    ) -> int:
        part = await self._database.fetchone(
            'SELECT part.id,part.job_id,part.xml_path,part.danmaku_import_state,'
            'part.cid,job.policy_snapshot_json FROM upload_parts part '
            'JOIN upload_jobs job ON job.id=part.job_id '
            'JOIN recording_sessions session ON session.id=job.session_id '
            "WHERE part.id=? AND session.deletion_state='none'",
            (part_id,),
        )
        if part is None:
            raise ValueError("unknown upload part '{}'".format(part_id))
        if str(part['danmaku_import_state']) == 'completed':
            return 0
        path = Path(xml_path or str(part['xml_path'] or ''))
        if not path.is_file():
            await self._mark_part_missing(part_id, int(part['job_id']))
            return 0
        if self._positive_int(part['cid']) is None:
            raise ValueError('danmaku part has no CID')
        job_id = int(part['job_id'])
        loop = asyncio.get_running_loop()
        try:
            filters = danmaku_filter or self._snapshot_filter(
                str(part['policy_snapshot_json'])
            )
            identity = await loop.run_in_executor(None, self.xml_identity, path)
            await self._reject_changed_source(part_id, identity)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._mark_part_failed(part_id, job_id)
            raise
        await self._database.execute(
            "UPDATE upload_parts SET danmaku_import_state='importing' "
            "WHERE id=? AND danmaku_import_state IN "
            "('pending','importing','waiting_capacity')",
            (part_id,),
        )

        imported = 0
        waiting = False
        iterator = self.parse(path, filters)
        executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='blrec-danmaku-xml'
        )
        try:
            while True:
                if not self._has_disk_capacity(path):
                    waiting = True
                    break
                batch = await loop.run_in_executor(
                    executor, self._next_batch, iterator, self._insert_batch_size
                )
                if not batch:
                    break
                added, capacity_reached = await self._insert_batch(
                    part_id, identity, batch
                )
                imported += added
                if capacity_reached:
                    waiting = True
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._mark_part_failed(part_id, job_id)
            raise
        finally:
            await loop.run_in_executor(executor, iterator.close)
            executor.shutdown(wait=True)

        next_state = 'waiting_capacity' if waiting else 'completed'
        await self._database.execute(
            'UPDATE upload_parts SET danmaku_import_state=? WHERE id=?',
            (next_state, part_id),
        )
        await self._refresh_job_state(job_id)
        return imported

    @classmethod
    def parse(
        cls, xml_path: Union[str, Path], filters: DanmakuFilter
    ) -> Generator[ImportedDanmaku, None, None]:
        seen_event_ids: Set[str] = set()
        original_index = 0
        parser = etree.iterparse(
            str(xml_path), events=('end',), resolve_entities=False, no_network=True
        )
        for _event, element in parser:
            if element.tag in ('d', 'sc', 'guard'):
                row = cls._element(element, original_index)
                original_index += 1
                if row is not None:
                    source_event_id = row.source_event_id
                    repeated = bool(
                        source_event_id and source_event_id in seen_event_ids
                    )
                    if source_event_id:
                        seen_event_ids.add(source_event_id)
                    if not repeated and filters.allows(row):
                        yield row
            element.clear()
            parent = element.getparent()
            if parent is not None:
                while element.getprevious() is not None:
                    del parent[0]

    @classmethod
    def xml_identity(cls, xml_path: Union[str, Path]) -> str:
        path = Path(xml_path).resolve(strict=True)
        before = path.stat()
        with path.open('rb') as file:
            head = file.read(cls._IDENTITY_BLOCK_SIZE)
            if before.st_size > cls._IDENTITY_BLOCK_SIZE:
                file.seek(max(0, before.st_size - cls._IDENTITY_BLOCK_SIZE))
                tail = file.read(cls._IDENTITY_BLOCK_SIZE)
            else:
                tail = b''
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('danmaku XML changed while computing identity')
        return json.dumps(
            {
                'path': str(path),
                'size': after.st_size,
                'mtime_ns': after.st_mtime_ns,
                'head_tail_sha256': hashlib.sha256(head + b'\0' + tail).hexdigest(),
            },
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        )

    async def _insert_batch(
        self, part_id: int, xml_identity: str, rows: Sequence[ImportedDanmaku]
    ) -> Tuple[int, bool]:
        def insert(connection: sqlite3.Connection) -> Tuple[int, bool]:
            placeholders = ','.join('?' for _ in self._ACTIVE_STATES)
            active = int(
                connection.execute(
                    'SELECT COUNT(*) FROM danmaku_items '
                    'WHERE state IN ({})'.format(placeholders),
                    self._ACTIVE_STATES,
                ).fetchone()[0]
            )
            inserted = 0
            for row in rows:
                if active >= self._import_high_watermark:
                    exists = connection.execute(
                        'SELECT 1 FROM danmaku_items '
                        'WHERE part_id=? AND xml_identity=? AND original_index=?',
                        (part_id, xml_identity, row.original_index),
                    ).fetchone()
                    if exists is not None:
                        continue
                    return inserted, True
                cursor = connection.execute(
                    'INSERT OR IGNORE INTO danmaku_items('
                    'part_id,xml_identity,original_index,progress_ms,mode,'
                    'fontsize,color,content,priority,request_fingerprint,state) '
                    "VALUES(?,?,?,?,?,?,?,?,?,?,'prepared')",
                    (
                        part_id,
                        xml_identity,
                        row.original_index,
                        row.progress_ms,
                        row.mode,
                        row.fontsize,
                        row.color,
                        row.content,
                        row.priority,
                        self._fingerprint(part_id, xml_identity, row),
                    ),
                )
                if cursor.rowcount == 1:
                    inserted += 1
                    active += 1
            return inserted, False

        return await self._database.write(insert)

    async def _reject_changed_source(self, part_id: int, identity: str) -> None:
        rows = await self._database.fetchall(
            'SELECT DISTINCT xml_identity FROM danmaku_items WHERE part_id=?',
            (part_id,),
        )
        if rows and any(str(row['xml_identity']) != identity for row in rows):
            raise ValueError('danmaku XML changed after import started')

    async def _active_count(self) -> int:
        placeholders = ','.join('?' for _ in self._ACTIVE_STATES)
        return int(
            await self._database.scalar(
                'SELECT COUNT(*) FROM danmaku_items '
                'WHERE state IN ({})'.format(placeholders),
                self._ACTIVE_STATES,
            )
        )

    async def _refresh_job_state(self, job_id: int) -> None:
        states = {
            str(row['danmaku_import_state'])
            for row in await self._database.fetchall(
                'SELECT danmaku_import_state FROM upload_parts WHERE job_id=?',
                (job_id,),
            )
        }
        active_states = {'pending', 'importing', 'waiting_capacity'}
        if 'failed' in states:
            branch_state = 'failed'
        elif states & active_states:
            branch_state = 'importing'
        elif states:
            item_count = int(
                await self._database.scalar(
                    'SELECT COUNT(*) FROM danmaku_items item '
                    'JOIN upload_parts part ON part.id=item.part_id '
                    'WHERE part.job_id=?',
                    (job_id,),
                )
            )
            if item_count:
                branch_state = 'publishing'
            elif 'missing_source' in states:
                branch_state = 'skipped_source_missing'
            else:
                branch_state = 'completed'
        else:
            branch_state = 'completed'
        await self._database.execute(
            'UPDATE upload_jobs SET danmaku_branch_state=?,updated_at=? '
            "WHERE id=? AND state='approved' AND danmaku_branch_state IN "
            "('pending','importing','publishing')",
            (branch_state, int(self._clock()), job_id),
        )

    async def _mark_job_missing(self, job_id: int, part_ids: Sequence[int]) -> None:
        now = int(self._clock())

        def mark(connection: sqlite3.Connection) -> None:
            for part_id in part_ids:
                connection.execute(
                    "UPDATE upload_parts SET danmaku_import_state='missing_source' "
                    'WHERE id=? AND job_id=?',
                    (part_id, job_id),
                )
            connection.execute(
                'UPDATE upload_jobs SET '
                "danmaku_branch_state='skipped_source_missing',updated_at=? "
                "WHERE id=? AND state='approved' AND danmaku_branch_state='pending'",
                (now, job_id),
            )

        await self._database.write(mark)

    async def _mark_part_missing(self, part_id: int, job_id: int) -> None:
        await self._database.execute(
            "UPDATE upload_parts SET danmaku_import_state='missing_source' "
            'WHERE id=?',
            (part_id,),
        )
        await self._refresh_job_state(job_id)

    async def _mark_part_failed(self, part_id: int, job_id: int) -> None:
        await self._database.execute(
            "UPDATE upload_parts SET danmaku_import_state='failed' WHERE id=?",
            (part_id,),
        )
        await self._refresh_job_state(job_id)

    def _has_disk_capacity(self, xml_path: Path) -> bool:
        locations = {
            Path(self._database.path).parent.resolve(),
            xml_path.parent.resolve(),
        }
        return all(
            self._free_space(location) > self._space_threshold_bytes
            for location in locations
        )

    @classmethod
    def _element(cls, element: Any, original_index: int) -> Optional[ImportedDanmaku]:
        if element.tag == 'd':
            params = str(element.get('p') or '').split(',')
            if len(params) < 4:
                raise ValueError('danmaku XML has an invalid p attribute')
            progress_ms = cls._progress_ms(params[0])
            content = cls._clean(element.text or '')
            if progress_ms is None or not content:
                return None
            return ImportedDanmaku(
                original_index=original_index,
                progress_ms=progress_ms,
                mode=int(params[1]),
                fontsize=int(params[2]),
                color=int(params[3]),
                content=content,
                priority=0,
                source_event_id=cls._text(element.get('source_event_id')),
                is_system=cls._optional_bool(element.get('is_system')),
                is_lottery=cls._optional_bool(element.get('is_lottery')),
                user_level=cls._optional_int(element.get('user_level')),
                fan_medal_level=cls._optional_int(element.get('fan_medal_level')),
            )
        progress_ms = cls._progress_ms(element.get('ts'))
        if progress_ms is None:
            return None
        user = cls._clean(element.get('user') or '') or '未知用户'
        if element.tag == 'sc':
            raw_price = int(float(cls._required(element, 'price')))
            content = '{}发送了{}元留言：{}'.format(
                user, raw_price // 1000, cls._clean(element.text or '')
            )
        else:
            gift_name = cls._clean(element.get('giftname') or '') or '舰长'
            count = cls._positive_int(element.get('count')) or 1
            months = '{}个月'.format(count) if count > 1 else ''
            content = '{}开通了{}{}'.format(user, months, gift_name)
        return ImportedDanmaku(
            original_index=original_index,
            progress_ms=progress_ms,
            mode=5,
            fontsize=25,
            color=16_776_960,
            content=cls._limit_content(content),
            priority=100,
        )

    @staticmethod
    def _next_batch(
        iterator: Iterator[ImportedDanmaku], size: int
    ) -> List[ImportedDanmaku]:
        return list(islice(iterator, size))

    @staticmethod
    def _snapshot_filter(snapshot_json: str) -> DanmakuFilter:
        try:
            snapshot = json.loads(snapshot_json)
        except (TypeError, ValueError) as error:
            raise ValueError('upload policy snapshot is invalid') from error
        if not isinstance(snapshot, Mapping):
            raise ValueError('upload policy snapshot is invalid')
        return DanmakuFilter.from_policy(snapshot.get('filters'))

    @staticmethod
    def _fingerprint(part_id: int, xml_identity: str, row: ImportedDanmaku) -> str:
        payload: Dict[str, Any] = {
            'part_id': part_id,
            'xml_identity': xml_identity,
            'original_index': row.original_index,
            'progress_ms': row.progress_ms,
            'mode': row.mode,
            'fontsize': row.fontsize,
            'color': row.color,
            'content': row.content,
        }
        return hashlib.sha256(
            json.dumps(
                payload, ensure_ascii=False, separators=(',', ':'), sort_keys=True
            ).encode('utf8')
        ).hexdigest()

    @staticmethod
    def _progress_ms(value: Any) -> Optional[int]:
        try:
            progress = int(float(value) * 1000)
        except (TypeError, ValueError):
            return None
        return progress if progress >= 0 else None

    @staticmethod
    def _required(element: Any, name: str) -> str:
        value = element.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError("danmaku XML is missing '{}'".format(name))
        return value

    @staticmethod
    def _clean(value: str) -> str:
        return ''.join(
            character for character in value if unicodedata.category(character) != 'Cc'
        ).strip()

    @staticmethod
    def _limit_content(value: str) -> str:
        suffix = '……（内容过长已截断）'
        return value if len(value) <= 100 else value[: 100 - len(suffix)] + suffix

    @staticmethod
    def _text(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None

    @staticmethod
    def _optional_bool(value: Any) -> Optional[bool]:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if normalized in ('1', 'true'):
            return True
        if normalized in ('0', 'false'):
            return False
        return None

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _positive_int(cls, value: Any) -> Optional[int]:
        result = cls._optional_int(value)
        return result if result is not None and result > 0 else None

    @staticmethod
    def _disk_free(path: Path) -> int:
        return int(shutil.disk_usage(str(path)).free)
