from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    cast,
)

from loguru import logger

from blrec.bili_upload.database import BiliUploadDatabase

from .models import (
    RecentVisit,
    VisitorAnalyticsFilters,
    VisitorAnalyticsSummary,
    VisitorAnalyticsTotals,
    VisitorDimensionPoint,
    VisitorTrendPoint,
)

if TYPE_CHECKING:
    from .sls import VisitorAnalyticsConfig, VisitorAnalyticsQuery

_CHINA_TIMEZONE = timezone(timedelta(hours=8))


class VisitorArchiveSource(Protocol):
    async def archive_page(
        self, from_time: int, to_time: int, *, offset: int, limit: int
    ) -> Sequence[Mapping[str, object]]:
        pass


@dataclass(frozen=True)
class ArchivedVisitorEvent:
    request_id: str
    occurred_at: int
    event: str
    visitor_hash: str
    page: str
    source: str
    device: str
    browser: str
    country: str
    province: str
    city: str
    provider: str


@dataclass(frozen=True)
class VisitorArchiveStatus:
    synced_through: int
    initial_sync_complete: bool
    first_event_at: Optional[int]
    last_started_at: Optional[int]
    last_completed_at: Optional[int]
    last_error: Optional[str]


class VisitorAnalyticsArchive:
    _dimension_columns = {
        'pages': 'page',
        'countries': 'country',
        'provinces': 'province',
        'cities': 'city',
        'providers': 'provider',
        'sources': 'source',
        'devices': 'device',
        'browsers': 'browser',
    }

    def __init__(self, database: BiliUploadDatabase) -> None:
        self._database = database

    async def status(self) -> VisitorArchiveStatus:
        def read(connection: sqlite3.Connection) -> VisitorArchiveStatus:
            row = connection.execute(
                'SELECT synced_through,initial_sync_complete,last_started_at,'
                'last_completed_at,last_error,'
                '(SELECT MIN(occurred_at) FROM visitor_analytics_events) '
                'AS first_event_at FROM visitor_analytics_sync_state '
                'WHERE singleton_id=1'
            ).fetchone()
            if row is None:
                raise RuntimeError('visitor analytics sync state is missing')
            return VisitorArchiveStatus(
                synced_through=int(row['synced_through']),
                initial_sync_complete=bool(row['initial_sync_complete']),
                first_event_at=(
                    None
                    if row['first_event_at'] is None
                    else int(row['first_event_at'])
                ),
                last_started_at=(
                    None
                    if row['last_started_at'] is None
                    else int(row['last_started_at'])
                ),
                last_completed_at=(
                    None
                    if row['last_completed_at'] is None
                    else int(row['last_completed_at'])
                ),
                last_error=(
                    None if row['last_error'] is None else str(row['last_error'])
                ),
            )

        return await self._database.read(read)

    async def insert(self, events: Sequence[ArchivedVisitorEvent]) -> int:
        if not events:
            return 0

        def write(connection: sqlite3.Connection) -> int:
            before = connection.total_changes
            connection.executemany(
                'INSERT OR IGNORE INTO visitor_analytics_events('
                'request_id,occurred_at,event,visitor_hash,page,source,device,'
                'browser,country,province,city,provider) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                [
                    (
                        item.request_id,
                        item.occurred_at,
                        item.event,
                        item.visitor_hash,
                        item.page,
                        item.source,
                        item.device,
                        item.browser,
                        item.country,
                        item.province,
                        item.city,
                        item.provider,
                    )
                    for item in events
                ],
            )
            return connection.total_changes - before

        return await self._database.write(write)

    async def mark_started(self, started_at: int) -> None:
        await self._database.execute(
            'UPDATE visitor_analytics_sync_state SET last_started_at=?,last_error=NULL '
            'WHERE singleton_id=1',
            (started_at,),
        )

    async def advance(self, synced_through: int) -> None:
        await self._database.execute(
            'UPDATE visitor_analytics_sync_state '
            'SET synced_through=MAX(synced_through,?) WHERE singleton_id=1',
            (synced_through,),
        )

    async def mark_completed(self, synced_through: int, completed_at: int) -> None:
        await self._database.execute(
            'UPDATE visitor_analytics_sync_state SET '
            'synced_through=MAX(synced_through,?),initial_sync_complete=1,'
            'last_completed_at=?,last_error=NULL WHERE singleton_id=1',
            (synced_through, completed_at),
        )

    async def mark_failed(self, error: str) -> None:
        await self._database.execute(
            'UPDATE visitor_analytics_sync_state SET last_error=? '
            'WHERE singleton_id=1',
            (error.strip()[:500],),
        )

    async def summary(
        self,
        query: VisitorAnalyticsQuery,
        config: VisitorAnalyticsConfig,
        *,
        generated_at: datetime,
        status: Optional[VisitorArchiveStatus] = None,
    ) -> VisitorAnalyticsSummary:
        archive_status = status or await self.status()
        where, parameters = _local_where(query)
        granularity: Literal['hour', 'day'] = (
            'hour'
            if query.end_at.timestamp() - query.start_at.timestamp() <= 172800
            else 'day'
        )

        def read(connection: sqlite3.Connection) -> Dict[str, object]:
            totals = connection.execute(
                'SELECT COUNT(*) AS events,COUNT(DISTINCT visitor_hash) '
                'AS visitors,SUM(CASE WHEN event=\'pageview\' THEN 1 ELSE 0 END) '
                'AS page_views,SUM(CASE WHEN event=\'heartbeat\' THEN 1 ELSE 0 END) '
                'AS heartbeats FROM visitor_analytics_events WHERE ' + where,
                parameters,
            ).fetchone()
            bucket_format = '%Y-%m-%d %H:00' if granularity == 'hour' else '%Y-%m-%d'
            trend = connection.execute(
                'SELECT strftime(?,occurred_at,\'unixepoch\',\'+8 hours\') AS '
                'bucket,COUNT(*) AS events,COUNT(DISTINCT visitor_hash) '
                'AS visitors FROM visitor_analytics_events WHERE '
                + where
                + ' GROUP BY bucket ORDER BY bucket ASC LIMIT 2000',
                (bucket_format,) + parameters,
            ).fetchall()
            dimensions = {
                name: connection.execute(
                    'SELECT "{}" AS dimension,COUNT(*) AS events,'
                    'COUNT(DISTINCT visitor_hash) AS visitors '
                    'FROM visitor_analytics_events WHERE {} GROUP BY "{}" '
                    'ORDER BY visitors DESC,events DESC LIMIT 20'.format(
                        column, where, column
                    ),
                    parameters,
                ).fetchall()
                for name, column in self._dimension_columns.items()
            }
            recent = connection.execute(
                'SELECT occurred_at,visitor_hash,page,source,device,browser,'
                'country,province,city FROM visitor_analytics_events WHERE '
                + where
                + ' ORDER BY occurred_at DESC,request_id DESC LIMIT 50',
                parameters,
            ).fetchall()
            return {
                'totals': totals,
                'trend': trend,
                'dimensions': dimensions,
                'recent': recent,
            }

        values = await self._database.read(read)
        totals = values['totals']
        assert isinstance(totals, sqlite3.Row)
        trend = cast(Sequence[sqlite3.Row], values['trend'])
        dimensions = cast(Dict[str, Sequence[sqlite3.Row]], values['dimensions'])
        recent = cast(Sequence[sqlite3.Row], values['recent'])
        warnings: List[str] = []
        if not archive_status.initial_sync_complete:
            warnings.append('本地访问日志仍在首次同步，较早数据可能暂不完整')
        if archive_status.last_error:
            warnings.append(
                '本地访问日志同步失败：{}'.format(archive_status.last_error)
            )
        if not config.configured:
            warnings.append('SLS 只读凭据未配置，本地访问日志无法继续更新')
        if (
            archive_status.first_event_at is not None
            and int(query.start_at.timestamp()) < archive_status.first_event_at
        ):
            warnings.append(
                '本地明细归档始于 {}'.format(
                    _required_datetime(archive_status.first_event_at)
                    .astimezone(_CHINA_TIMEZONE)
                    .strftime('%Y-%m-%d %H:%M')
                )
            )
        return VisitorAnalyticsSummary(
            provider='aliyun-sls',
            status='partial' if archive_status.last_error else 'ready',
            configured=config.configured,
            generated_at=generated_at,
            retention_days=config.retention_days,
            cache_seconds=config.cache_seconds,
            archive_enabled=True,
            archive_initial_sync_complete=archive_status.initial_sync_complete,
            archive_start_at=_datetime(archive_status.first_event_at),
            archive_synced_through=_datetime(archive_status.synced_through),
            archive_last_completed_at=_datetime(archive_status.last_completed_at),
            archive_last_error=archive_status.last_error,
            filters=_filters(query),
            totals=VisitorAnalyticsTotals(
                visitors=_row_integer(totals, 'visitors'),
                events=_row_integer(totals, 'events'),
                page_views=_row_integer(totals, 'page_views'),
                heartbeats=_row_integer(totals, 'heartbeats'),
            ),
            trend_granularity=granularity,
            trend=[
                VisitorTrendPoint(
                    bucket=str(row['bucket']),
                    visitors=_row_integer(row, 'visitors'),
                    events=_row_integer(row, 'events'),
                )
                for row in trend
            ],
            pages=_dimensions(dimensions['pages']),
            countries=_dimensions(dimensions['countries']),
            provinces=_dimensions(dimensions['provinces']),
            cities=_dimensions(dimensions['cities']),
            providers=_dimensions(dimensions['providers']),
            sources=_dimensions(dimensions['sources']),
            devices=_dimensions(dimensions['devices']),
            browsers=_dimensions(dimensions['browsers']),
            recent_visits=[
                RecentVisit(
                    occurred_at=_required_datetime(int(row['occurred_at']))
                    .astimezone(_CHINA_TIMEZONE)
                    .strftime('%Y-%m-%d %H:%M:%S'),
                    visitor='#{}'.format(str(row['visitor_hash'])[:8]),
                    page=str(row['page']),
                    source=str(row['source']),
                    device=str(row['device']),
                    browser=str(row['browser']),
                    country=str(row['country']),
                    province=str(row['province']),
                    city=str(row['city']),
                )
                for row in recent
            ],
            warnings=warnings,
        )


class VisitorAnalyticsSynchronizer:
    def __init__(
        self,
        config: VisitorAnalyticsConfig,
        source: VisitorArchiveSource,
        archive: VisitorAnalyticsArchive,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        interval_seconds: int = 300,
        ingestion_delay_seconds: int = 120,
        overlap_seconds: int = 3600,
        window_seconds: int = 3600,
        page_size: int = 100,
    ) -> None:
        self._config = config
        self._source = source
        self._archive = archive
        self._now = now
        self._interval_seconds = interval_seconds
        self._ingestion_delay_seconds = ingestion_delay_seconds
        self._overlap_seconds = overlap_seconds
        self._window_seconds = window_seconds
        self._page_size = page_size
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        task, self._task = self._task, None
        self._stop.set()
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def sync_once(self) -> int:
        if not self._config.configured:
            return 0
        async with self._lock:
            status = await self._archive.status()
            current = int(self._now().timestamp())
            target = current - self._ingestion_delay_seconds
            retention_start = target - self._config.retention_days * 86400 + 1
            if status.synced_through <= 0:
                cursor = retention_start
            elif status.initial_sync_complete:
                cursor = max(
                    retention_start, status.synced_through - self._overlap_seconds
                )
            else:
                cursor = max(retention_start, status.synced_through)
            await self._archive.mark_started(current)
            inserted = 0
            try:
                while cursor < target:
                    window_end = min(target, cursor + self._window_seconds)
                    inserted += await self._sync_window(cursor, window_end)
                    await self._archive.advance(window_end)
                    cursor = window_end
                await self._archive.mark_completed(target, int(self._now().timestamp()))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._archive.mark_failed(str(error))
                raise
            return inserted

    async def _sync_window(self, from_time: int, to_time: int) -> int:
        offset = 0
        inserted = 0
        while True:
            values = await self._source.archive_page(
                from_time, to_time, offset=offset, limit=self._page_size
            )
            events = [
                event
                for event in (_event(value) for value in values)
                if event is not None
            ]
            inserted += await self._archive.insert(events)
            if len(values) < self._page_size:
                return inserted
            offset += self._page_size

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                inserted = await self.sync_once()
                if inserted:
                    logger.info(
                        'Archived visitor analytics events: inserted={}', inserted
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Visitor analytics archive sync failed')
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_seconds
                )
            except asyncio.TimeoutError:
                pass


def _event(value: Mapping[str, object]) -> Optional[ArchivedVisitorEvent]:
    event = _text(value.get('event'), 20)
    if event not in ('pageview', 'heartbeat'):
        return None
    try:
        occurred_at = int(float(str(value.get('occurred_at') or 0)))
    except (TypeError, ValueError):
        return None
    visitor = _text(value.get('visitor'), 128)
    if occurred_at <= 0 or not visitor:
        return None
    fields = {
        'page': _text(value.get('page'), 128, 'unknown'),
        'source': _text(value.get('source'), 128, 'unknown'),
        'device': _text(value.get('device'), 32, 'unknown'),
        'browser': _text(value.get('browser'), 64, '其他'),
        'country': _text(value.get('country'), 64, '未知'),
        'province': _text(value.get('province'), 64, '未知'),
        'city': _text(value.get('city'), 64, '未知'),
        'provider': _text(value.get('provider'), 64, '未知'),
    }
    request_id = _text(value.get('request_id'), 128)
    if not request_id:
        fingerprint = '\0'.join(
            [str(occurred_at), event, visitor] + [fields[key] for key in sorted(fields)]
        )
        request_id = hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()
    return ArchivedVisitorEvent(
        request_id=request_id,
        occurred_at=occurred_at,
        event=event,
        visitor_hash=hashlib.sha256(visitor.encode('utf-8')).hexdigest(),
        **fields,
    )


def _text(value: object, limit: int, default: str = '') -> str:
    cleaned = str(value or '').strip()[:limit]
    return cleaned or default


def _local_where(query: VisitorAnalyticsQuery) -> Tuple[str, Tuple[object, ...]]:
    clauses = ['occurred_at>=?', 'occurred_at<?']
    parameters: List[object] = [
        int(query.start_at.timestamp()),
        int(query.end_at.timestamp()),
    ]
    if query.event != 'all':
        clauses.append('event=?')
        parameters.append(query.event)
    for value, column in (
        (query.page, 'page'),
        (query.country, 'country'),
        (query.province, 'province'),
        (query.city, 'city'),
        (query.provider, 'provider'),
        (query.source, 'source'),
        (query.device, 'device'),
        (query.browser, 'browser'),
    ):
        if value is not None:
            clauses.append('"{}"=?'.format(column))
            parameters.append(value)
    return ' AND '.join(clauses), tuple(parameters)


def _datetime(value: Optional[int]) -> Optional[datetime]:
    if value is None or value <= 0:
        return None
    return datetime.fromtimestamp(value, timezone.utc)


def _required_datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value, timezone.utc)


def _row_integer(row: sqlite3.Row, key: str) -> int:
    value = row[key]
    return 0 if value is None else max(0, int(value))


def _dimensions(rows: Sequence[sqlite3.Row]) -> List[VisitorDimensionPoint]:
    return [
        VisitorDimensionPoint(
            value=str(row['dimension']),
            visitors=_row_integer(row, 'visitors'),
            events=_row_integer(row, 'events'),
        )
        for row in rows
    ]


def _filters(query: VisitorAnalyticsQuery) -> VisitorAnalyticsFilters:
    return VisitorAnalyticsFilters(
        start_at=query.start_at,
        end_at=query.end_at,
        event=query.event,
        page=query.page,
        country=query.country,
        province=query.province,
        city=query.city,
        provider=query.provider,
        source=query.source,
        device=query.device,
        browser=query.browser,
    )
