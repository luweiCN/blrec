#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, Optional, Protocol

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - exercised by the Python 3.8 CI job
    from backports.zoneinfo import ZoneInfo

LOGGER = logging.getLogger('site-stats-publisher')
SHANGHAI = ZoneInfo('Asia/Shanghai')
HISTORY_SCHEMA_VERSION = 1
PUBLIC_SCHEMA_VERSION = 1
ACTIVE_WINDOW_MINUTES = 5
REFRESH_CALENDAR_DAYS = 7
ANALYTICS_PATH = '/analytics/pixel.svg'
ANALYTICS_DOMAIN = 'vg.luwei.host'
PAGEVIEW_PARAM_PATTERN = r'^[?]event=pageview&visitor=[0-9a-f-]{16,64}$'
ACTIVE_PARAM_PATTERN = r'^[?]event=(pageview|heartbeat)&visitor=[0-9a-f-]{16,64}$'
HISTORY_OBJECT_KEY = 'data/site-stats-history.json'
PUBLIC_OBJECT_KEY = 'data/site-stats.json'


class SiteStatsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Counts:
    page_views: int
    visitors: int


@dataclass
class StatsHistory:
    tracking_started_at: datetime
    days: Dict[str, Counts]


class AnalyticsSource(Protocol):
    def daily_counts(self, from_time: int, to_time: int) -> Counts:
        pass

    def active_visitors(self, from_time: int, to_time: int) -> int:
        pass


class StatsStore(Protocol):
    def load_history(self) -> Optional[bytes]:
        pass

    def publish_history(self, contents: bytes) -> None:
        pass

    def publish_public_stats(self, contents: bytes) -> None:
        pass


def _is_non_negative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise SiteStatsError('{}必须是 ISO 8601 时间'.format(label))
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SiteStatsError('{}不是有效的 ISO 8601 时间'.format(label)) from exc
    if parsed.tzinfo is None:
        raise SiteStatsError('{}必须包含时区'.format(label))
    return parsed.astimezone(SHANGHAI)


def _datetime_json(value: datetime) -> str:
    return value.astimezone(SHANGHAI).isoformat(timespec='seconds')


def parse_history(contents: bytes) -> StatsHistory:
    try:
        value = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SiteStatsError('OSS 中的访问统计历史文件无法解析') from exc
    if not isinstance(value, dict) or value.get('schemaVersion') != 1:
        raise SiteStatsError('OSS 中的访问统计历史文件版本不受支持')
    if value.get('timezone') != 'Asia/Shanghai':
        raise SiteStatsError('OSS 中的访问统计历史文件时区不受支持')
    tracking_started_at = _parse_datetime(
        value.get('trackingStartedAt'), 'trackingStartedAt'
    )
    raw_days = value.get('days')
    if not isinstance(raw_days, dict):
        raise SiteStatsError('OSS 中的访问统计历史文件缺少 days')

    days: Dict[str, Counts] = {}
    for day_key, raw_counts in raw_days.items():
        if not isinstance(day_key, str):
            raise SiteStatsError('访问统计历史日期必须是字符串')
        try:
            parsed_day = date.fromisoformat(day_key)
        except ValueError as exc:
            raise SiteStatsError('访问统计历史包含无效日期') from exc
        if parsed_day < tracking_started_at.date():
            raise SiteStatsError('访问统计历史包含早于统计开始时间的日期')
        if not isinstance(raw_counts, dict):
            raise SiteStatsError('访问统计历史包含无效计数')
        page_views = raw_counts.get('pageViews')
        visitors = raw_counts.get('visitors')
        if not _is_non_negative_integer(page_views) or not _is_non_negative_integer(
            visitors
        ):
            raise SiteStatsError('访问统计历史包含无效计数')
        days[day_key] = Counts(page_views=page_views, visitors=visitors)
    return StatsHistory(tracking_started_at=tracking_started_at, days=days)


def serialize_history(history: StatsHistory) -> bytes:
    value = {
        'schemaVersion': HISTORY_SCHEMA_VERSION,
        'timezone': 'Asia/Shanghai',
        'trackingStartedAt': _datetime_json(history.tracking_started_at),
        'days': {
            day_key: {'visitors': counts.visitors, 'pageViews': counts.page_views}
            for day_key, counts in sorted(history.days.items())
        },
    }
    return _json_bytes(value)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        + '\n'
    ).encode('utf-8')


def _refresh_days(now: datetime, tracking_started_at: datetime):
    today = now.astimezone(SHANGHAI).date()
    earliest_retained_day = today - timedelta(days=REFRESH_CALENDAR_DAYS - 1)
    first_day = max(earliest_retained_day, tracking_started_at.date())
    current = first_day
    while current <= today:
        yield current
        current += timedelta(days=1)


def _day_bounds(
    day: date, now: datetime, tracking_started_at: datetime
) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=SHANGHAI)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=SHANGHAI)
    start = max(start, tracking_started_at)
    end = min(end, now.astimezone(SHANGHAI) + timedelta(seconds=1))
    return start, end


def _assert_not_regressed(day_key: str, previous: Counts, current: Counts) -> None:
    if current.page_views < previous.page_views or current.visitors < previous.visitors:
        raise SiteStatsError(
            '{} 的访问统计少于上次结果，已停止发布以保护累计数据'.format(day_key)
        )


def refresh_site_stats(
    analytics: AnalyticsSource,
    store: StatsStore,
    tracking_started_at: datetime,
    now: datetime,
) -> dict:
    if tracking_started_at.tzinfo is None or now.tzinfo is None:
        raise SiteStatsError('统计开始时间和当前时间必须包含时区')
    tracking_started_at = tracking_started_at.astimezone(SHANGHAI)
    now = now.astimezone(SHANGHAI)
    if tracking_started_at > now:
        raise SiteStatsError('统计开始时间不能晚于当前时间')

    history_contents = store.load_history()
    history = (
        parse_history(history_contents)
        if history_contents is not None
        else StatsHistory(tracking_started_at=tracking_started_at, days={})
    )
    if history.tracking_started_at != tracking_started_at:
        raise SiteStatsError('配置的统计开始时间与 OSS 历史文件不一致')
    if any(date.fromisoformat(day_key) > now.date() for day_key in history.days):
        raise SiteStatsError('访问统计历史包含未来日期')

    for day in _refresh_days(now, tracking_started_at):
        start, end = _day_bounds(day, now, tracking_started_at)
        if start >= end:
            continue
        day_key = day.isoformat()
        counts = analytics.daily_counts(int(start.timestamp()), int(end.timestamp()))
        previous = history.days.get(day_key)
        if previous is not None:
            _assert_not_regressed(day_key, previous, counts)
        history.days[day_key] = counts

    active_from = int((now - timedelta(minutes=ACTIVE_WINDOW_MINUTES)).timestamp())
    active_to = int((now + timedelta(seconds=1)).timestamp())
    active_visitors = analytics.active_visitors(active_from, active_to)
    if not _is_non_negative_integer(active_visitors):
        raise SiteStatsError('近 5 分钟活跃人数不是有效计数')

    today_key = now.date().isoformat()
    today = history.days.get(today_key, Counts(page_views=0, visitors=0))
    public_stats = {
        'schemaVersion': PUBLIC_SCHEMA_VERSION,
        'generatedAt': _datetime_json(now),
        'timezone': 'Asia/Shanghai',
        'trackingStartedAt': _datetime_json(tracking_started_at),
        'activeWindowMinutes': ACTIVE_WINDOW_MINUTES,
        'today': {
            'date': today_key,
            'visitors': today.visitors,
            'pageViews': today.page_views,
        },
        'activeVisitors': active_visitors,
        'totalPageViews': sum(item.page_views for item in history.days.values()),
    }

    store.publish_history(serialize_history(history))
    store.publish_public_stats(_json_bytes(public_stats))
    return public_stats


class AliyunSlsAnalytics:
    _search = 'domain: "{}" and uri: "{}"'.format(ANALYTICS_DOMAIN, ANALYTICS_PATH)
    _visitor_expression = "regexp_extract(uri_param, 'visitor=([0-9a-f-]{16,64})', 1)"

    def __init__(
        self,
        endpoint: str,
        project: str,
        logstore: str,
        access_key_id: str,
        access_key_secret: str,
    ) -> None:
        try:
            from aliyun.log import GetLogsRequest, LogClient
        except ImportError as exc:
            raise SiteStatsError(
                '缺少阿里云 SLS SDK，请安装 site-stats-requirements.txt'
            ) from exc
        self._client = LogClient(endpoint, access_key_id, access_key_secret)
        self._request_type = GetLogsRequest
        self._project = project
        self._logstore = logstore

    def daily_counts(self, from_time: int, to_time: int) -> Counts:
        query = (
            self._search
            + ' | SELECT count(*) AS page_views, count(DISTINCT '
            + self._visitor_expression
            + ") AS visitors FROM log WHERE regexp_like(uri_param, "
            + "'{}')".format(PAGEVIEW_PARAM_PATTERN)
        )
        result = self._query(from_time, to_time, query)
        return Counts(
            page_views=_count_field(result, 'page_views'),
            visitors=_count_field(result, 'visitors'),
        )

    def active_visitors(self, from_time: int, to_time: int) -> int:
        query = (
            self._search
            + ' | SELECT count(DISTINCT '
            + self._visitor_expression
            + ") AS visitors FROM log WHERE regexp_like(uri_param, "
            + "'{}')".format(ACTIVE_PARAM_PATTERN)
        )
        return _count_field(self._query(from_time, to_time, query), 'visitors')

    def _query(self, from_time: int, to_time: int, query: str) -> dict:
        request = self._request_type(
            self._project, self._logstore, from_time, to_time, query=query, line=1
        )
        response = self._client.get_logs(request)
        if not response.is_completed():
            raise SiteStatsError('SLS 查询在重试后仍未完成')
        logs = response.get_logs()
        if len(logs) != 1:
            raise SiteStatsError('SLS 聚合查询没有返回唯一结果')
        return logs[0].get_contents()


def _count_field(value: dict, field: str) -> int:
    try:
        parsed = int(value[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise SiteStatsError('SLS 返回的 {} 不是有效计数'.format(field)) from exc
    if parsed < 0:
        raise SiteStatsError('SLS 返回的 {} 不是有效计数'.format(field))
    return parsed


class AliyunOssStatsStore:
    def __init__(
        self,
        endpoint: str,
        bucket_name: str,
        access_key_id: str,
        access_key_secret: str,
    ) -> None:
        try:
            import oss2
        except ImportError as exc:
            raise SiteStatsError(
                '缺少阿里云 OSS SDK，请安装 site-stats-requirements.txt'
            ) from exc
        self._oss2 = oss2
        self._bucket = oss2.Bucket(
            oss2.Auth(access_key_id, access_key_secret), endpoint, bucket_name
        )

    def load_history(self) -> Optional[bytes]:
        try:
            return self._bucket.get_object(HISTORY_OBJECT_KEY).read()
        except self._oss2.exceptions.NoSuchKey:
            return None

    def publish_history(self, contents: bytes) -> None:
        self._bucket.put_object(
            HISTORY_OBJECT_KEY,
            contents,
            headers={
                'Content-Type': 'application/json; charset=utf-8',
                'Cache-Control': 'private, no-store, max-age=0',
            },
        )

    def publish_public_stats(self, contents: bytes) -> None:
        self._bucket.put_object(
            PUBLIC_OBJECT_KEY,
            contents,
            headers={
                'Content-Type': 'application/json; charset=utf-8',
                'Cache-Control': 'no-store, max-age=0',
            },
        )


def _required_environment(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value:
        raise SiteStatsError('缺少环境变量 {}'.format(name))
    return value


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    try:
        access_key_id = _required_environment('ALIBABA_CLOUD_ACCESS_KEY_ID')
        access_key_secret = _required_environment('ALIBABA_CLOUD_ACCESS_KEY_SECRET')
        analytics = AliyunSlsAnalytics(
            endpoint=_required_environment('SLS_ENDPOINT'),
            project=_required_environment('SLS_PROJECT'),
            logstore=_required_environment('SLS_LOGSTORE'),
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
        )
        store = AliyunOssStatsStore(
            endpoint=_required_environment('OSS_ENDPOINT'),
            bucket_name=_required_environment('OSS_BUCKET'),
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
        )
        stats = refresh_site_stats(
            analytics=analytics,
            store=store,
            tracking_started_at=_parse_datetime(
                _required_environment('SITE_STATS_TRACKING_STARTED_AT'),
                'SITE_STATS_TRACKING_STARTED_AT',
            ),
            now=datetime.now(tz=SHANGHAI),
        )
    except Exception as exc:
        LOGGER.error('%s', exc)
        return 1

    LOGGER.info(
        'published site stats: date=%s visitors=%s page_views=%s active=%s total=%s',
        stats['today']['date'],
        stats['today']['visitors'],
        stats['today']['pageViews'],
        stats['activeVisitors'],
        stats['totalPageViews'],
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
