from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)
from urllib.parse import quote, urlencode, urlsplit

from .models import (
    RecentVisit,
    VisitorAnalyticsFilters,
    VisitorAnalyticsSummary,
    VisitorAnalyticsTotals,
    VisitorDimensionPoint,
    VisitorTrendPoint,
)

_CHINA_TIMEZONE = timezone(timedelta(hours=8))
_EVENT_EXPRESSION = "regexp_extract(uri_param, '(?:^|[?&])kind=([^&]*)', 1)"
_VISITOR_EXPRESSION = (
    "regexp_extract(uri_param, '(?:^|[?&])visitor=([0-9a-f-]{16,64})', 1)"
)
_PAGE_EXPRESSION = (
    "coalesce(nullif(regexp_extract(uri_param, "
    "'(?:^|[?&])page=([^&]*)', 1), ''), 'unknown')"
)
_SOURCE_EXPRESSION = (
    "coalesce(nullif(regexp_extract(uri_param, "
    "'(?:^|[?&])source=([^&]*)', 1), ''), 'unknown')"
)
_DEVICE_EXPRESSION = (
    "coalesce(nullif(regexp_extract(uri_param, "
    "'(?:^|[?&])device=([^&]*)', 1), ''), "
    "CASE WHEN regexp_like(lower(user_agent), 'ipad|tablet') THEN 'tablet' "
    "WHEN regexp_like(lower(user_agent), 'mobile|android|iphone') THEN 'mobile' "
    "ELSE 'desktop' END)"
)
_BROWSER_EXPRESSION = (
    "CASE WHEN regexp_like(lower(user_agent), 'edg/') THEN 'Edge' "
    "WHEN regexp_like(lower(user_agent), 'micromessenger') THEN '微信' "
    "WHEN regexp_like(lower(user_agent), 'qqbrowser') THEN 'QQ 浏览器' "
    "WHEN regexp_like(lower(user_agent), 'ucbrowser') THEN 'UC 浏览器' "
    "WHEN regexp_like(lower(user_agent), 'firefox/') THEN 'Firefox' "
    "WHEN regexp_like(lower(user_agent), 'chrome|crios') THEN 'Chrome' "
    "WHEN regexp_like(lower(user_agent), 'safari/') THEN 'Safari' "
    "ELSE '其他' END"
)
_COUNTRY_EXPRESSION = "coalesce(nullif(ip_to_country(client_ip), ''), '未知')"
_PROVINCE_EXPRESSION = "coalesce(nullif(ip_to_province(client_ip), ''), '未知')"
_CITY_EXPRESSION = "coalesce(nullif(ip_to_city(client_ip), ''), '未知')"
_PROVIDER_EXPRESSION = "coalesce(nullif(ip_to_provider(client_ip), ''), '未知')"
_VALID_VISITOR = "regexp_like(uri_param, '(^|[?&])visitor=[0-9a-f-]{16,64}(&|$)')"
_DETAIL_EVENT = "regexp_like(uri_param, '(^|[?&])event=detail(&|$)')"


class HttpSession(Protocol):
    def get(self, url: str, **kwargs: object) -> Any:
        pass


class SlsQuery(Protocol):
    async def query(
        self, from_time: int, to_time: int, query: str, *, line: int = 100
    ) -> Sequence[Mapping[str, object]]:
        pass


@dataclass(frozen=True)
class VisitorAnalyticsConfig:
    access_key_id: Optional[str]
    access_key_secret: Optional[str]
    endpoint: str = 'cn-beijing.log.aliyuncs.com'
    project: str = 'vainglory'
    logstore: str = 'vainglory-dashboard'
    domain: str = 'vg.luwei.host'
    cache_seconds: int = 300
    retention_days: int = 7

    @classmethod
    def from_env(cls) -> 'VisitorAnalyticsConfig':
        cache_seconds = _bounded_env_int(
            'BLREC_VISITOR_ANALYTICS_CACHE_SECONDS', 300, 60, 1800
        )
        retention_days = _bounded_env_int(
            'BLREC_VISITOR_ANALYTICS_RETENTION_DAYS', 7, 1, 31
        )
        return cls(
            access_key_id=(
                os.environ.get('BLREC_VISITOR_ANALYTICS_ALIYUN_ACCESS_KEY_ID')
                or os.environ.get('BLREC_CLOUD_COST_ALIYUN_ACCESS_KEY_ID')
                or None
            ),
            access_key_secret=(
                os.environ.get('BLREC_VISITOR_ANALYTICS_ALIYUN_ACCESS_KEY_SECRET')
                or os.environ.get('BLREC_CLOUD_COST_ALIYUN_ACCESS_KEY_SECRET')
                or None
            ),
            endpoint=os.environ.get(
                'BLREC_VISITOR_ANALYTICS_SLS_ENDPOINT', 'cn-beijing.log.aliyuncs.com'
            ),
            project=os.environ.get('BLREC_VISITOR_ANALYTICS_SLS_PROJECT', 'vainglory'),
            logstore=os.environ.get(
                'BLREC_VISITOR_ANALYTICS_SLS_LOGSTORE', 'vainglory-dashboard'
            ),
            domain=os.environ.get('BLREC_VISITOR_ANALYTICS_DOMAIN', 'vg.luwei.host'),
            cache_seconds=cache_seconds,
            retention_days=retention_days,
        )

    @property
    def configured(self) -> bool:
        return bool(
            self.access_key_id
            and self.access_key_secret
            and self.endpoint
            and self.project
            and self.logstore
        )

    def missing_environment_variables(self) -> List[str]:
        missing: List[str] = []
        if not self.access_key_id:
            missing.append('BLREC_VISITOR_ANALYTICS_ALIYUN_ACCESS_KEY_ID')
        if not self.access_key_secret:
            missing.append('BLREC_VISITOR_ANALYTICS_ALIYUN_ACCESS_KEY_SECRET')
        return missing


@dataclass(frozen=True)
class VisitorAnalyticsQuery:
    start_at: datetime
    end_at: datetime
    event: Literal['all', 'pageview', 'heartbeat'] = 'pageview'
    page: Optional[str] = None
    country: Optional[str] = None
    province: Optional[str] = None
    city: Optional[str] = None
    provider: Optional[str] = None
    source: Optional[str] = None
    device: Optional[str] = None
    browser: Optional[str] = None

    def normalized(self) -> 'VisitorAnalyticsQuery':
        return VisitorAnalyticsQuery(
            start_at=_aware(self.start_at),
            end_at=_aware(self.end_at),
            event=self.event,
            page=_clean_filter(self.page),
            country=_clean_filter(self.country),
            province=_clean_filter(self.province),
            city=_clean_filter(self.city),
            provider=_clean_filter(self.provider),
            source=_clean_filter(self.source),
            device=_clean_filter(self.device),
            browser=_clean_filter(self.browser),
        )

    def cache_key(self) -> Tuple[object, ...]:
        return (
            int(self.start_at.timestamp()),
            int(self.end_at.timestamp()),
            self.event,
            self.page,
            self.country,
            self.province,
            self.city,
            self.provider,
            self.source,
            self.device,
            self.browser,
        )


class AliyunSlsQueryError(RuntimeError):
    pass


def build_sls_authorization(
    *,
    method: str,
    resource: str,
    parameters: Mapping[str, object],
    headers: Mapping[str, str],
    access_key_id: str,
    access_key_secret: str,
) -> str:
    normalized_headers = {key.lower(): value.strip() for key, value in headers.items()}
    canonical_headers = ''.join(
        '{}:{}\n'.format(key, normalized_headers[key])
        for key in sorted(normalized_headers)
        if key.startswith('x-log-') or key.startswith('x-acs-')
    )
    canonical_resource = resource
    if parameters:
        canonical_resource += '?' + '&'.join(
            '{}={}'.format(key, parameters[key]) for key in sorted(parameters)
        )
    message = '{}\n{}\n{}\n{}\n{}{}'.format(
        method.upper(),
        normalized_headers.get('content-md5', ''),
        normalized_headers.get('content-type', ''),
        normalized_headers['date'],
        canonical_headers,
        canonical_resource,
    )
    signature = base64.b64encode(
        hmac.new(
            access_key_secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha1
        ).digest()
    ).decode('ascii')
    return 'LOG {}:{}'.format(access_key_id, signature)


class AliyunSlsQueryClient:
    def __init__(
        self,
        config: VisitorAnalyticsConfig,
        session_provider: Callable[[], HttpSession],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._config = config
        self._session_provider = session_provider
        self._now = now

    async def query(
        self, from_time: int, to_time: int, query: str, *, line: int = 100
    ) -> Sequence[Mapping[str, object]]:
        endpoint = _endpoint_host(self._config.endpoint)
        host = '{}.{}'.format(self._config.project, endpoint)
        resource = '/logstores/{}'.format(quote(self._config.logstore, safe=''))
        parameters: Dict[str, object] = {
            'accurate': 'true',
            'from': from_time,
            'line': line,
            'offset': 0,
            'powerSql': 'true',
            'query': query,
            'reverse': 'true',
            'to': to_time,
            'type': 'log',
        }
        request_time = _aware(self._now()).astimezone(timezone.utc)
        headers = {
            'Date': format_datetime(request_time, usegmt=True),
            'Host': host,
            'x-log-apiversion': '0.6.0',
            'x-log-bodyrawsize': '0',
            'x-log-signaturemethod': 'hmac-sha1',
        }
        headers['Authorization'] = build_sls_authorization(
            method='GET',
            resource=resource,
            parameters=parameters,
            headers=headers,
            access_key_id=self._config.access_key_id or '',
            access_key_secret=self._config.access_key_secret or '',
        )
        headers['x-log-date'] = headers['Date']
        encoded_parameters = {key: str(value) for key, value in parameters.items()}
        url = 'https://{}{}?{}'.format(host, resource, urlencode(encoded_parameters))
        async with self._session_provider().get(url, headers=headers) as response:
            payload = await response.json(content_type=None)
            progress = response.headers.get('x-log-progress', 'Complete')
        if progress.lower() != 'complete':
            raise AliyunSlsQueryError('SLS 查询尚未完成，请稍后重试')
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, Mapping)]
        if isinstance(payload, Mapping):
            logs = payload.get('logs') or payload.get('data')
            if isinstance(logs, list):
                return [item for item in logs if isinstance(item, Mapping)]
            code = payload.get('errorCode') or payload.get('Code')
            message = payload.get('errorMessage') or payload.get('Message')
            if code or message:
                raise AliyunSlsQueryError('{}：{}'.format(code, message))
        raise AliyunSlsQueryError('SLS 返回了无法识别的数据')


class VisitorAnalyticsService:
    _dimension_expressions = {
        'pages': _PAGE_EXPRESSION,
        'countries': _COUNTRY_EXPRESSION,
        'provinces': _PROVINCE_EXPRESSION,
        'cities': _CITY_EXPRESSION,
        'providers': _PROVIDER_EXPRESSION,
        'sources': _SOURCE_EXPRESSION,
        'devices': _DEVICE_EXPRESSION,
        'browsers': _BROWSER_EXPRESSION,
    }

    def __init__(
        self,
        config: VisitorAnalyticsConfig,
        client: SlsQuery,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._client = client
        self._now = now
        self._clock = clock
        self._cache_lock = asyncio.Lock()
        self._cache: Dict[Tuple[object, ...], Tuple[float, VisitorAnalyticsSummary]] = (
            {}
        )

    async def summary(
        self, query: VisitorAnalyticsQuery, *, force_refresh: bool = False
    ) -> VisitorAnalyticsSummary:
        normalized = query.normalized()
        self._validate_range(normalized)
        cache_key = normalized.cache_key()
        if not force_refresh:
            cached = self._fresh_cache(cache_key)
            if cached is not None:
                return cached
        async with self._cache_lock:
            if not force_refresh:
                cached = self._fresh_cache(cache_key)
                if cached is not None:
                    return cached
            result = await self._fetch_summary(normalized)
            self._prune_cache()
            self._cache[cache_key] = (self._clock(), result)
            return result

    def _validate_range(self, query: VisitorAnalyticsQuery) -> None:
        if query.end_at <= query.start_at:
            raise ValueError('结束时间必须晚于开始时间')
        maximum = timedelta(days=self._config.retention_days)
        if query.end_at - query.start_at > maximum:
            raise ValueError(
                '日志只保留最近 {} 天，查询范围不能更长'.format(
                    self._config.retention_days
                )
            )

    def _fresh_cache(
        self, key: Tuple[object, ...]
    ) -> Optional[VisitorAnalyticsSummary]:
        cached = self._cache.get(key)
        if cached is None:
            return None
        cached_at, value = cached
        if self._clock() - cached_at >= self._config.cache_seconds:
            return None
        return value

    def _prune_cache(self) -> None:
        expired = [
            key
            for key, (cached_at, _value) in self._cache.items()
            if self._clock() - cached_at >= self._config.cache_seconds
        ]
        for key in expired:
            self._cache.pop(key, None)
        if len(self._cache) >= 64:
            oldest = min(self._cache, key=lambda key: self._cache[key][0])
            self._cache.pop(oldest, None)

    async def _fetch_summary(
        self, query: VisitorAnalyticsQuery
    ) -> VisitorAnalyticsSummary:
        filters = _model_filters(query)
        now = _aware(self._now()).astimezone(timezone.utc)
        if not self._config.configured:
            return VisitorAnalyticsSummary(
                provider='aliyun-sls',
                status='not_configured',
                configured=False,
                generated_at=now,
                retention_days=self._config.retention_days,
                cache_seconds=self._config.cache_seconds,
                filters=filters,
                totals=VisitorAnalyticsTotals(),
                warnings=[
                    '尚未配置 {}'.format(
                        '、'.join(self._config.missing_environment_variables())
                    )
                ],
            )

        from_time = int(query.start_at.timestamp())
        to_time = int(query.end_at.timestamp())
        where = _where_clause(query)
        search = 'domain: "{}" and uri: "/analytics/pixel.svg"'.format(
            self._config.domain.replace('"', '')
        )
        granularity: Literal['hour', 'day'] = (
            'hour' if query.end_at - query.start_at <= timedelta(days=2) else 'day'
        )
        operations: List[Tuple[str, Awaitable[Sequence[Mapping[str, object]]]]] = [
            (
                'totals',
                self._client.query(
                    from_time, to_time, _totals_sql(search, where), line=1
                ),
            ),
            (
                'trend',
                self._client.query(
                    from_time, to_time, _trend_sql(search, where, granularity), line=200
                ),
            ),
        ]
        operations.extend(
            (
                name,
                self._client.query(
                    from_time,
                    to_time,
                    _dimension_sql(search, where, expression),
                    line=20,
                ),
            )
            for name, expression in self._dimension_expressions.items()
        )
        operations.append(
            (
                'recent_visits',
                self._client.query(
                    from_time, to_time, _recent_sql(search, where), line=50
                ),
            )
        )
        values = await asyncio.gather(
            *(operation for _name, operation in operations), return_exceptions=True
        )
        results: Dict[str, Sequence[Mapping[str, object]]] = {}
        warnings: List[str] = []
        for (name, _operation), value in zip(operations, values):
            if isinstance(value, BaseException):
                warnings.append('{} 查询失败：{}'.format(_query_label(name), value))
            else:
                results[name] = value

        totals = _parse_totals(results.get('totals', []))
        dimensions = {
            name: _parse_dimensions(results.get(name, []))
            for name in self._dimension_expressions
        }
        if not warnings:
            status: Literal['ready', 'partial', 'error'] = 'ready'
        elif results:
            status = 'partial'
        else:
            status = 'error'
        return VisitorAnalyticsSummary(
            provider='aliyun-sls',
            status=status,
            configured=True,
            generated_at=now,
            retention_days=self._config.retention_days,
            cache_seconds=self._config.cache_seconds,
            filters=filters,
            totals=totals,
            trend_granularity=granularity,
            trend=_parse_trend(results.get('trend', [])),
            pages=dimensions['pages'],
            countries=dimensions['countries'],
            provinces=dimensions['provinces'],
            cities=dimensions['cities'],
            providers=dimensions['providers'],
            sources=dimensions['sources'],
            devices=dimensions['devices'],
            browsers=dimensions['browsers'],
            recent_visits=_parse_recent_visits(results.get('recent_visits', [])),
            warnings=warnings,
        )


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.environ.get(name, str(default)))))
    except ValueError:
        return default


def _endpoint_host(endpoint: str) -> str:
    value = endpoint.strip().rstrip('/')
    if '://' in value:
        value = urlsplit(value).netloc
    return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_CHINA_TIMEZONE)
    return value


def _clean_filter(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()[:128]
    return cleaned or None


def _sql_literal(value: str) -> str:
    return "'{}'".format(value.replace("'", "''"))


def _where_clause(query: VisitorAnalyticsQuery) -> str:
    clauses = [_DETAIL_EVENT, _VALID_VISITOR]
    if query.event != 'all':
        clauses.append('{} = {}'.format(_EVENT_EXPRESSION, _sql_literal(query.event)))
    filters = (
        (query.page, _PAGE_EXPRESSION),
        (query.country, _COUNTRY_EXPRESSION),
        (query.province, _PROVINCE_EXPRESSION),
        (query.city, _CITY_EXPRESSION),
        (query.provider, _PROVIDER_EXPRESSION),
        (query.source, _SOURCE_EXPRESSION),
        (query.device, _DEVICE_EXPRESSION),
        (query.browser, _BROWSER_EXPRESSION),
    )
    clauses.extend(
        '{} = {}'.format(expression, _sql_literal(value))
        for value, expression in filters
        if value is not None
    )
    return ' AND '.join(clauses)


def _totals_sql(search: str, where: str) -> str:
    return (
        search
        + ' | SELECT count(*) AS events, count(DISTINCT '
        + _VISITOR_EXPRESSION
        + ') AS visitors, sum(CASE WHEN '
        + _EVENT_EXPRESSION
        + " = 'pageview' THEN 1 ELSE 0 END) AS page_views, "
        + 'sum(CASE WHEN '
        + _EVENT_EXPRESSION
        + " = 'heartbeat' THEN 1 ELSE 0 END) AS heartbeats FROM log WHERE "
        + where
    )


def _trend_sql(search: str, where: str, granularity: Literal['hour', 'day']) -> str:
    pattern = '%Y-%m-%d %H:00' if granularity == 'hour' else '%Y-%m-%d'
    bucket = "date_format(from_unixtime(__time__), '{}')".format(pattern)
    return (
        search
        + ' | SELECT '
        + bucket
        + ' AS bucket, count(*) AS events, count(DISTINCT '
        + _VISITOR_EXPRESSION
        + ') AS visitors FROM log WHERE '
        + where
        + ' GROUP BY '
        + bucket
        + ' ORDER BY bucket ASC LIMIT 200'
    )


def _dimension_sql(search: str, where: str, expression: str) -> str:
    return (
        search
        + ' | SELECT '
        + expression
        + ' AS dimension, count(*) AS events, count(DISTINCT '
        + _VISITOR_EXPRESSION
        + ') AS visitors FROM log WHERE '
        + where
        + ' GROUP BY '
        + expression
        + ' ORDER BY visitors DESC, events DESC LIMIT 20'
    )


def _recent_sql(search: str, where: str) -> str:
    return (
        search
        + " | SELECT date_format(from_unixtime(__time__), '%Y-%m-%d %H:%i:%s') "
        + 'AS occurred_at, '
        + _VISITOR_EXPRESSION
        + ' AS visitor, '
        + _PAGE_EXPRESSION
        + ' AS page, '
        + _SOURCE_EXPRESSION
        + ' AS source, '
        + _DEVICE_EXPRESSION
        + ' AS device, '
        + _BROWSER_EXPRESSION
        + ' AS browser, '
        + _COUNTRY_EXPRESSION
        + ' AS country, '
        + _PROVINCE_EXPRESSION
        + ' AS province, '
        + _CITY_EXPRESSION
        + ' AS city FROM log WHERE '
        + where
        + ' ORDER BY __time__ DESC LIMIT 50'
    )


def _integer(item: Mapping[str, object], key: str) -> int:
    try:
        return max(0, int(float(str(item.get(key, 0)))))
    except (TypeError, ValueError):
        return 0


def _parse_totals(values: Sequence[Mapping[str, object]]) -> VisitorAnalyticsTotals:
    item = values[0] if values else {}
    return VisitorAnalyticsTotals(
        visitors=_integer(item, 'visitors'),
        events=_integer(item, 'events'),
        page_views=_integer(item, 'page_views'),
        heartbeats=_integer(item, 'heartbeats'),
    )


def _parse_trend(values: Sequence[Mapping[str, object]]) -> List[VisitorTrendPoint]:
    result = [
        VisitorTrendPoint(
            bucket=str(item.get('bucket') or ''),
            visitors=_integer(item, 'visitors'),
            events=_integer(item, 'events'),
        )
        for item in values
        if item.get('bucket')
    ]
    result.sort(key=lambda item: item.bucket)
    return result


def _parse_dimensions(
    values: Sequence[Mapping[str, object]]
) -> List[VisitorDimensionPoint]:
    return [
        VisitorDimensionPoint(
            value=str(item.get('dimension') or '未知'),
            visitors=_integer(item, 'visitors'),
            events=_integer(item, 'events'),
        )
        for item in values
    ]


def _parse_recent_visits(values: Sequence[Mapping[str, object]]) -> List[RecentVisit]:
    result: List[RecentVisit] = []
    for item in values:
        raw_visitor = str(item.get('visitor') or '')
        visitor = hashlib.sha256(raw_visitor.encode('utf-8')).hexdigest()[:8]
        result.append(
            RecentVisit(
                occurred_at=str(item.get('occurred_at') or ''),
                visitor='#{}'.format(visitor),
                page=str(item.get('page') or 'unknown'),
                source=str(item.get('source') or 'unknown'),
                device=str(item.get('device') or 'unknown'),
                browser=str(item.get('browser') or '其他'),
                country=str(item.get('country') or '未知'),
                province=str(item.get('province') or '未知'),
                city=str(item.get('city') or '未知'),
            )
        )
    return result


def _model_filters(query: VisitorAnalyticsQuery) -> VisitorAnalyticsFilters:
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


def _query_label(name: str) -> str:
    return {
        'totals': '汇总',
        'trend': '趋势',
        'pages': '页面分布',
        'countries': '国家分布',
        'provinces': '省份分布',
        'cities': '城市分布',
        'providers': '运营商分布',
        'sources': '来源分布',
        'devices': '设备分布',
        'browsers': '浏览器分布',
        'recent_visits': '最近访问',
    }.get(name, name)
