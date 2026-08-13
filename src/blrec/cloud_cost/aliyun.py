from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    MutableMapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)
from urllib.parse import quote
from xml.etree import ElementTree

from .models import (
    BillableUsageItem,
    CdnDailyUsage,
    CdnUsage,
    CloudCostSummary,
    CostTotals,
    CostTrendPoint,
    DailyCost,
    OssUsage,
    ProductCost,
)

_SIGNATURE_ALGORITHM = 'ACS3-HMAC-SHA256'
_EMPTY_PAYLOAD_HASH = hashlib.sha256(b'').hexdigest()
_BSS_ENDPOINT = 'business.aliyuncs.com'
_BSS_VERSION = '2017-12-14'
_CDN_ENDPOINT = 'cdn.aliyuncs.com'
_CDN_VERSION = '2018-05-10'
_OSS_SIGNATURE_ALGORITHM = 'OSS4-HMAC-SHA256'
_OSS_UNSIGNED_PAYLOAD = 'UNSIGNED-PAYLOAD'
_CHINA_TIMEZONE = timezone(timedelta(hours=8))
_BSS_REQUEST_INTERVAL_SECONDS = 0.12


class HttpSession(Protocol):
    def post(self, url: str, **kwargs: object) -> Any:
        pass

    def get(self, url: str, **kwargs: object) -> Any:
        pass


class OpenApiCaller(Protocol):
    async def call(
        self, endpoint: str, action: str, version: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        pass


class OssStatCaller(Protocol):
    async def get_bucket_stat(
        self, *, bucket: str, endpoint: str, region: str
    ) -> Mapping[str, object]:
        pass


@dataclass(frozen=True)
class CloudCostConfig:
    access_key_id: Optional[str]
    access_key_secret: Optional[str]
    cdn_domain: str = 'vg.luwei.host'
    oss_bucket: str = 'luwei-vainglory'
    oss_endpoint: str = 'oss-cn-beijing.aliyuncs.com'
    oss_region: str = 'cn-beijing'
    cache_seconds: int = 600

    @classmethod
    def from_env(cls) -> 'CloudCostConfig':
        cache_value = os.environ.get('BLREC_CLOUD_COST_CACHE_SECONDS', '600')
        try:
            cache_seconds = max(60, min(3600, int(cache_value)))
        except ValueError:
            cache_seconds = 600
        return cls(
            access_key_id=(
                os.environ.get('BLREC_CLOUD_COST_ALIYUN_ACCESS_KEY_ID') or None
            ),
            access_key_secret=(
                os.environ.get('BLREC_CLOUD_COST_ALIYUN_ACCESS_KEY_SECRET') or None
            ),
            cdn_domain=os.environ.get(
                'BLREC_CLOUD_COST_ALIYUN_CDN_DOMAIN', 'vg.luwei.host'
            ),
            oss_bucket=os.environ.get(
                'BLREC_CLOUD_COST_ALIYUN_OSS_BUCKET', 'luwei-vainglory'
            ),
            oss_endpoint=os.environ.get(
                'BLREC_CLOUD_COST_ALIYUN_OSS_ENDPOINT', 'oss-cn-beijing.aliyuncs.com'
            ),
            oss_region=os.environ.get(
                'BLREC_CLOUD_COST_ALIYUN_OSS_REGION', 'cn-beijing'
            ),
            cache_seconds=cache_seconds,
        )

    @property
    def configured(self) -> bool:
        return bool(self.access_key_id and self.access_key_secret)

    def missing_environment_variables(self) -> List[str]:
        missing: List[str] = []
        if not self.access_key_id:
            missing.append('BLREC_CLOUD_COST_ALIYUN_ACCESS_KEY_ID')
        if not self.access_key_secret:
            missing.append('BLREC_CLOUD_COST_ALIYUN_ACCESS_KEY_SECRET')
        return missing


class AliyunOpenApiError(RuntimeError):
    def __init__(self, action: str, code: str, message: str) -> None:
        super().__init__('{} failed ({}): {}'.format(action, code, message))


def _encode(value: object) -> str:
    if isinstance(value, bool):
        text = 'true' if value else 'false'
    else:
        text = str(value)
    return quote(text, safe='-_.~')


def canonical_query(parameters: Mapping[str, object]) -> str:
    return '&'.join(
        '{}={}'.format(_encode(key), _encode(value))
        for key, value in sorted(parameters.items())
        if value is not None
    )


def canonical_oss_query(parameters: Mapping[str, object]) -> str:
    parts: List[str] = []
    for key, value in sorted(parameters.items()):
        encoded_key = _encode(key)
        parts.append(
            encoded_key
            if value is None
            else '{}={}'.format(encoded_key, _encode(value))
        )
    return '&'.join(parts)


def build_acs3_authorization(
    *,
    method: str,
    canonical_uri: str,
    query: Mapping[str, object],
    headers: Mapping[str, str],
    access_key_id: str,
    access_key_secret: str,
) -> str:
    signed = {
        key.lower(): value.strip()
        for key, value in headers.items()
        if key.lower() == 'host'
        or key.lower() == 'content-type'
        or key.lower().startswith('x-acs-')
    }
    signed_headers = ';'.join(sorted(signed))
    canonical_headers = ''.join(
        '{}:{}\n'.format(key, signed[key]) for key in sorted(signed)
    )
    canonical_request = '{}\n{}\n{}\n{}\n{}\n{}'.format(
        method.upper(),
        canonical_uri,
        canonical_query(query),
        canonical_headers,
        signed_headers,
        headers['x-acs-content-sha256'],
    )
    string_to_sign = '{}\n{}'.format(
        _SIGNATURE_ALGORITHM,
        hashlib.sha256(canonical_request.encode('utf-8')).hexdigest(),
    )
    signature = hmac.new(
        access_key_secret.encode('utf-8'),
        string_to_sign.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return '{} Credential={},SignedHeaders={},Signature={}'.format(
        _SIGNATURE_ALGORITHM, access_key_id, signed_headers, signature
    )


class AliyunOpenApiClient:
    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        session_provider: Callable[[], HttpSession],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        nonce: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._session_provider = session_provider
        self._now = now
        self._nonce = nonce

    async def call(
        self, endpoint: str, action: str, version: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        query = {key: value for key, value in parameters.items() if value is not None}
        request_time = self._now().astimezone(timezone.utc).replace(microsecond=0)
        headers = {
            'host': endpoint,
            'x-acs-action': action,
            'x-acs-content-sha256': _EMPTY_PAYLOAD_HASH,
            'x-acs-date': request_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'x-acs-signature-nonce': self._nonce(),
            'x-acs-version': version,
        }
        headers['Authorization'] = build_acs3_authorization(
            method='POST',
            canonical_uri='/',
            query=query,
            headers=headers,
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
        )
        url = 'https://{}/?{}'.format(endpoint, canonical_query(query))
        async with self._session_provider().post(
            url, headers=headers, data=b''
        ) as response:
            payload = await response.json(content_type=None)
        if not isinstance(payload, Mapping):
            raise AliyunOpenApiError(action, 'InvalidResponse', 'response is not JSON')
        success = payload.get('Success')
        code = str(payload.get('Code', ''))
        if success is False or (code and code != 'Success'):
            raise AliyunOpenApiError(
                action, code or 'Failed', str(payload.get('Message', 'unknown error'))
            )
        return payload


def build_oss4_authorization(
    *,
    method: str,
    bucket: str,
    region: str,
    query: Mapping[str, object],
    headers: Mapping[str, str],
    access_key_id: str,
    access_key_secret: str,
    canonical_key: str = '',
) -> str:
    timestamp = headers['x-oss-date']
    date = timestamp[:8]
    scope = '{}/{}/oss/aliyun_v4_request'.format(date, region)
    signed_headers = {
        key.lower(): value.strip()
        for key, value in headers.items()
        if key.lower().startswith('x-oss-')
        or key.lower() in ('content-md5', 'content-type')
        or key.lower() in ('content-disposition', 'content-length')
    }
    default_headers = {'content-md5', 'content-type'}
    additional_headers = sorted(
        set(signed_headers)
        - default_headers
        - {key for key in signed_headers if key.startswith('x-oss-')}
    )
    canonical_headers = ''.join(
        '{}:{}\n'.format(key, signed_headers[key]) for key in sorted(signed_headers)
    )
    canonical_uri = quote('/{}/{}'.format(bucket, canonical_key), safe='/-_.~')
    canonical_request = '{}\n{}\n{}\n{}\n{}\n{}'.format(
        method.upper(),
        canonical_uri,
        canonical_oss_query(query),
        canonical_headers,
        ';'.join(additional_headers),
        _OSS_UNSIGNED_PAYLOAD,
    )
    string_to_sign = '{}\n{}\n{}\n{}'.format(
        _OSS_SIGNATURE_ALGORITHM,
        timestamp,
        scope,
        hashlib.sha256(canonical_request.encode('utf-8')).hexdigest(),
    )
    date_key = hmac.new(
        ('aliyun_v4' + access_key_secret).encode('utf-8'),
        date.encode('utf-8'),
        hashlib.sha256,
    ).digest()
    region_key = hmac.new(date_key, region.encode('utf-8'), hashlib.sha256).digest()
    service_key = hmac.new(region_key, b'oss', hashlib.sha256).digest()
    signing_key = hmac.new(service_key, b'aliyun_v4_request', hashlib.sha256).digest()
    signature = hmac.new(
        signing_key, string_to_sign.encode('utf-8'), hashlib.sha256
    ).hexdigest()
    authorization = '{} Credential={}/{}'.format(
        _OSS_SIGNATURE_ALGORITHM, access_key_id, scope
    )
    if additional_headers:
        authorization += ',AdditionalHeaders={}'.format(';'.join(additional_headers))
    authorization += ',Signature={}'.format(signature)
    return authorization


class AliyunOssStatClient:
    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        session_provider: Callable[[], HttpSession],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._session_provider = session_provider
        self._now = now

    async def get_bucket_stat(
        self, *, bucket: str, endpoint: str, region: str
    ) -> Mapping[str, object]:
        request_time = self._now().astimezone(timezone.utc).replace(microsecond=0)
        host = '{}.{}'.format(bucket, endpoint)
        headers = {
            'host': host,
            'x-oss-content-sha256': _OSS_UNSIGNED_PAYLOAD,
            'x-oss-date': request_time.strftime('%Y%m%dT%H%M%SZ'),
        }
        headers['Authorization'] = build_oss4_authorization(
            method='GET',
            bucket=bucket,
            region=region,
            query={'stat': None},
            headers=headers,
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
        )
        async with self._session_provider().get(
            'https://{}/?stat'.format(host), headers=headers
        ) as response:
            body = await response.read()
            if response.status >= 400:
                code, message = _parse_oss_error(body)
                raise AliyunOpenApiError('GetBucketStat', code, message)
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as error:
            raise AliyunOpenApiError(
                'GetBucketStat', 'InvalidResponse', 'response is not XML'
            ) from error
        return {
            'Storage': _xml_int(root, 'Storage'),
            'ObjectCount': _xml_int(root, 'ObjectCount'),
            'LastModifiedTime': _xml_int(root, 'LastModifiedTime'),
        }


class AliyunCloudCostService:
    def __init__(
        self,
        config: CloudCostConfig,
        client: OpenApiCaller,
        oss_client: Optional[OssStatCaller] = None,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._client = client
        self._oss_client = oss_client
        self._now = now
        self._clock = clock
        self._sleeper = sleeper
        self._cache_lock = asyncio.Lock()
        self._cached_summary: Optional[CloudCostSummary] = None
        self._cached_at = 0.0
        self._bss_lock = asyncio.Lock()
        self._next_bss_request_at = 0.0

    async def summary(
        self, *, force_refresh: bool = False, billing_cycle: Optional[str] = None
    ) -> CloudCostSummary:
        selected_cycle = billing_cycle or self._current_billing_cycle()
        available_cycles = _billing_cycles_from(self._current_billing_cycle(), 18)
        if selected_cycle not in available_cycles:
            raise ValueError('billing_cycle must be within the latest 18 months')
        if not force_refresh and self._cache_is_fresh(selected_cycle):
            assert self._cached_summary is not None
            return self._cached_summary
        async with self._cache_lock:
            if not force_refresh and self._cache_is_fresh(selected_cycle):
                assert self._cached_summary is not None
                return self._cached_summary
            summary = await self._fetch_summary(selected_cycle)
            self._cached_summary = summary
            self._cached_at = self._clock()
            return summary

    def _cache_is_fresh(self, billing_cycle: str) -> bool:
        return self._cached_summary is not None and (
            self._cached_summary.billing_cycle == billing_cycle
            and self._clock() - self._cached_at < self._config.cache_seconds
        )

    async def _fetch_summary(self, billing_cycle: str) -> CloudCostSummary:
        now = self._normalized_now()
        if not self._config.configured:
            missing = '、'.join(self._config.missing_environment_variables())
            return CloudCostSummary(
                provider='aliyun',
                status='not_configured',
                configured=False,
                generated_at=now,
                billing_cycle=billing_cycle,
                cache_seconds=self._config.cache_seconds,
                totals=CostTotals(),
                warnings=['尚未配置 {}'.format(missing)],
            )

        available_cycles = _billing_cycles_from(self._current_billing_cycle(), 18)
        selected_cycle_index = available_cycles.index(billing_cycle)
        cycles = available_cycles[
            max(0, selected_cycle_index - 5) : selected_cycle_index + 1
        ]
        start_time, end_time = _cdn_time_range_for_cycle(now, billing_cycle)
        daily_dates = _billing_dates(billing_cycle, now)
        operations: List[Awaitable[Mapping[str, object]]] = [
            self._bill_overview(cycle) for cycle in cycles
        ]
        operations.extend(
            [
                self._oss_billable_usage(billing_cycle),
                self._oss_bucket_stat(),
                self._daily_bills(daily_dates),
                self._cdn_usage('traf', start_time=start_time, end_time=end_time),
                self._cdn_usage('acc', start_time=start_time, end_time=end_time),
            ]
        )
        results = await asyncio.gather(*operations, return_exceptions=True)
        overview_results = results[: len(cycles)]
        source_results = results[len(cycles) :]
        oss_result = source_results[0]
        oss_stat_result = source_results[1]
        daily_result = source_results[2]
        traffic_result, request_result = source_results[3:]
        warnings: List[str] = []
        successful_sources = 0

        trend: List[CostTrendPoint] = []
        current_products: List[ProductCost] = []
        currency = 'CNY'
        for cycle, result in zip(cycles, overview_results):
            if isinstance(result, BaseException):
                warnings.append('{} 账单查询失败：{}'.format(cycle, result))
                continue
            successful_sources += 1
            products, cycle_currency = _parse_products(result)
            if cycle == billing_cycle:
                current_products = products
                currency = cycle_currency
            totals = _sum_costs(products)
            trend.append(
                CostTrendPoint(
                    billing_cycle=cycle,
                    pretax_amount=totals.pretax_amount,
                    payment_amount=totals.payment_amount,
                )
            )

        totals = _sum_costs(current_products)
        oss_cost = _product_cost(current_products, ('oss', '对象存储'))
        cdn_cost = _product_cost(current_products, ('cdn', '内容分发'))

        oss_items: List[BillableUsageItem] = []
        if isinstance(oss_result, BaseException):
            warnings.append('OSS 用量查询失败：{}'.format(oss_result))
        else:
            successful_sources += 1
            oss_items = _parse_billable_usage(oss_result)
        oss_storage_bytes: Optional[int] = None
        oss_object_count: Optional[int] = None
        oss_storage_measured_at: Optional[datetime] = None
        if isinstance(oss_stat_result, BaseException):
            warnings.append('OSS 当前容量查询失败：{}'.format(oss_stat_result))
        else:
            successful_sources += 1
            oss_storage_bytes = _optional_int(oss_stat_result.get('Storage'))
            oss_object_count = _optional_int(oss_stat_result.get('ObjectCount'))
            measured_timestamp = _optional_int(oss_stat_result.get('LastModifiedTime'))
            if measured_timestamp is not None:
                oss_storage_measured_at = datetime.fromtimestamp(
                    measured_timestamp, tz=timezone.utc
                )
        oss = OssUsage(
            bucket=self._config.oss_bucket,
            storage_bytes=oss_storage_bytes,
            object_count=oss_object_count,
            storage_measured_at=oss_storage_measured_at,
            items=oss_items,
            pretax_amount=oss_cost.pretax_amount,
            payment_amount=oss_cost.payment_amount,
            outstanding_amount=oss_cost.outstanding_amount,
        )

        daily: List[DailyCost] = []
        if isinstance(daily_result, BaseException):
            warnings.append('日账单查询失败：{}'.format(daily_result))
        else:
            for value in _sequence(daily_result.get('Results')):
                result = _mapping(value)
                date = str(result.get('Date') or '')
                error = result.get('Error')
                if error:
                    warnings.append('{} 日账单查询失败：{}'.format(date, error))
                    continue
                successful_sources += 1
                daily_totals = _parse_daily_cost(_mapping(result.get('Payload')))
                daily.append(
                    DailyCost(
                        date=date,
                        pretax_amount=daily_totals.pretax_amount,
                        payment_amount=daily_totals.payment_amount,
                        outstanding_amount=daily_totals.outstanding_amount,
                    )
                )

        traffic: Dict[str, int] = {}
        requests: Dict[str, int] = {}
        if isinstance(traffic_result, BaseException):
            warnings.append('CDN 流量查询失败：{}'.format(traffic_result))
        else:
            successful_sources += 1
            traffic = _parse_cdn_usage(traffic_result)
        if isinstance(request_result, BaseException):
            warnings.append('CDN 请求数查询失败：{}'.format(request_result))
        else:
            successful_sources += 1
            requests = _parse_cdn_usage(request_result)
        dates = sorted(set(traffic) | set(requests))
        cdn = CdnUsage(
            domain=self._config.cdn_domain,
            traffic_bytes=sum(traffic.values()),
            requests=sum(requests.values()),
            daily=[
                CdnDailyUsage(
                    date=date,
                    traffic_bytes=traffic.get(date, 0),
                    requests=requests.get(date, 0),
                )
                for date in dates
            ],
            pretax_amount=cdn_cost.pretax_amount,
            payment_amount=cdn_cost.payment_amount,
            outstanding_amount=cdn_cost.outstanding_amount,
        )

        summary_status: Literal['ready', 'partial', 'error']
        if not warnings:
            summary_status = 'ready'
        elif successful_sources == 0:
            summary_status = 'error'
        else:
            summary_status = 'partial'
        return CloudCostSummary(
            provider='aliyun',
            status=summary_status,
            configured=True,
            generated_at=now,
            billing_cycle=billing_cycle,
            currency=currency,
            cache_seconds=self._config.cache_seconds,
            totals=totals,
            products=current_products,
            trend=trend,
            daily=daily,
            oss=oss,
            cdn=cdn,
            warnings=warnings,
        )

    def _normalized_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _current_billing_cycle(self) -> str:
        return self._normalized_now().astimezone(_CHINA_TIMEZONE).strftime('%Y-%m')

    async def _bill_overview(self, billing_cycle: str) -> Mapping[str, object]:
        return await self._bss_call(
            _BSS_ENDPOINT,
            'QueryBillOverview',
            _BSS_VERSION,
            {'BillingCycle': billing_cycle},
        )

    async def _oss_billable_usage(self, billing_cycle: str) -> Mapping[str, object]:
        items: List[object] = []
        next_token: Optional[str] = None
        seen_tokens = set()
        for _ in range(100):
            response = await self._bss_call(
                _BSS_ENDPOINT,
                'DescribeInstanceBill',
                _BSS_VERSION,
                {
                    'BillingCycle': billing_cycle,
                    'ProductCode': 'oss',
                    'IsBillingItem': True,
                    'IsHideZeroCharge': False,
                    'Granularity': 'MONTHLY',
                    'MaxResults': 300,
                    'NextToken': next_token,
                },
            )
            data = _mapping(response.get('Data'))
            items.extend(_sequence(data.get('Items')))
            token_value = data.get('NextToken')
            next_token = str(token_value) if token_value else None
            if next_token is None:
                break
            if next_token in seen_tokens:
                raise RuntimeError('DescribeInstanceBill returned a repeated token')
            seen_tokens.add(next_token)
        else:
            raise RuntimeError('DescribeInstanceBill exceeded 100 pages')
        return {'Data': {'Items': items}}

    async def _oss_bucket_stat(self) -> Mapping[str, object]:
        if self._oss_client is None:
            raise RuntimeError('OSS statistics client is unavailable')
        return await self._oss_client.get_bucket_stat(
            bucket=self._config.oss_bucket,
            endpoint=self._config.oss_endpoint,
            region=self._config.oss_region,
        )

    async def _daily_bill(self, billing_date: str) -> Mapping[str, object]:
        items: List[object] = []
        page = 1
        for _ in range(100):
            response = await self._bss_call(
                _BSS_ENDPOINT,
                'QueryAccountBill',
                _BSS_VERSION,
                {
                    'BillingCycle': billing_date[:7],
                    'BillingDate': billing_date,
                    'Granularity': 'DAILY',
                    'IsGroupByProduct': False,
                    'PageNum': page,
                    'PageSize': 300,
                },
            )
            data = _mapping(response.get('Data'))
            page_items = data.get('Items')
            values = (
                _sequence(page_items.get('Item'))
                if isinstance(page_items, Mapping)
                else _sequence(page_items)
            )
            items.extend(values)
            total_count = _optional_int(data.get('TotalCount'))
            if total_count is None or len(items) >= total_count:
                break
            page += 1
        else:
            raise RuntimeError('QueryAccountBill exceeded 100 pages')
        return {'Data': {'Items': {'Item': items}}}

    async def _daily_bills(self, billing_dates: Sequence[str]) -> Mapping[str, object]:
        results: List[Mapping[str, object]] = []
        for billing_date in billing_dates:
            try:
                payload = await self._daily_bill(billing_date)
            except Exception as error:
                results.append({'Date': billing_date, 'Error': str(error)})
            else:
                results.append({'Date': billing_date, 'Payload': payload})
        return {'Results': results}

    async def _bss_call(
        self, endpoint: str, action: str, version: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        async with self._bss_lock:
            delay = max(0.0, self._next_bss_request_at - self._clock())
            if delay:
                await self._sleeper(delay)
            self._next_bss_request_at = (
                max(self._next_bss_request_at, self._clock())
                + _BSS_REQUEST_INTERVAL_SECONDS
            )
            return await self._client.call(endpoint, action, version, parameters)

    async def _cdn_usage(
        self, field: str, *, start_time: str, end_time: str
    ) -> Mapping[str, object]:
        parameters: Dict[str, object] = {
            'DomainName': self._config.cdn_domain,
            'StartTime': start_time,
            'EndTime': end_time,
            'Field': field,
            'Type': 'all',
            'DataProtocol': 'all',
            'Interval': 3600,
        }
        if field != 'acc':
            parameters['Area'] = 'all'
        return await self._client.call(
            _CDN_ENDPOINT, 'DescribeDomainUsageData', _CDN_VERSION, parameters
        )


def _billing_cycles_from(billing_cycle: str, count: int) -> List[str]:
    year, month = (int(value) for value in billing_cycle.split('-', 1))
    values: List[str] = []
    for offset in range(count - 1, -1, -1):
        month_index = year * 12 + month - 1 - offset
        values.append('{:04d}-{:02d}'.format(month_index // 12, month_index % 12 + 1))
    return values


def _cdn_time_range_for_cycle(now: datetime, billing_cycle: str) -> Tuple[str, str]:
    local_now = now.astimezone(_CHINA_TIMEZONE)
    year, month = (int(value) for value in billing_cycle.split('-', 1))
    start = datetime(year, month, 1, tzinfo=_CHINA_TIMEZONE)
    if billing_cycle == local_now.strftime('%Y-%m'):
        end = local_now
    else:
        next_month_index = year * 12 + month
        end = datetime(
            next_month_index // 12, next_month_index % 12 + 1, 1, tzinfo=_CHINA_TIMEZONE
        )
    return (
        start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        end.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    )


def _billing_dates(billing_cycle: str, now: datetime) -> List[str]:
    year, month = (int(value) for value in billing_cycle.split('-', 1))
    local_today = now.astimezone(_CHINA_TIMEZONE).date()
    if billing_cycle == local_today.strftime('%Y-%m'):
        day_count = local_today.day
    else:
        next_month_index = year * 12 + month
        next_month = datetime(next_month_index // 12, next_month_index % 12 + 1, 1)
        day_count = (next_month - timedelta(days=1)).day
    return [
        '{:04d}-{:02d}-{:02d}'.format(year, month, day)
        for day in range(1, day_count + 1)
    ]


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, list) else []


def _overview_items(payload: Mapping[str, object]) -> Sequence[object]:
    data = _mapping(payload.get('Data'))
    items = data.get('Items')
    if isinstance(items, Mapping):
        return _sequence(items.get('Item'))
    return _sequence(items)


def _amount(item: Mapping[str, object], key: str) -> float:
    value = item.get(key, 0)
    try:
        return float(str(value)) if value not in (None, '') else 0
    except (TypeError, ValueError):
        return 0


def _optional_float(value: object) -> Optional[float]:
    if value in (None, ''):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> Optional[int]:
    if value in (None, ''):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _parse_products(payload: Mapping[str, object]) -> Tuple[List[ProductCost], str]:
    totals: MutableMapping[Tuple[str, str], CostTotals] = {}
    currency = 'CNY'
    for value in _overview_items(payload):
        item = _mapping(value)
        code = str(item.get('ProductCode') or item.get('PipCode') or 'unknown')
        name = str(item.get('ProductName') or item.get('ProductDetail') or code)
        currency = str(item.get('Currency') or currency)
        key = (code, name)
        current = totals.setdefault(key, CostTotals())
        current.pretax_amount += _amount(item, 'PretaxAmount')
        current.payment_amount += _amount(item, 'PaymentAmount')
        current.outstanding_amount += _amount(item, 'OutstandingAmount')
    products = [
        ProductCost(
            product_code=code,
            product_name=name,
            pretax_amount=value.pretax_amount,
            payment_amount=value.payment_amount,
            outstanding_amount=value.outstanding_amount,
        )
        for (code, name), value in totals.items()
    ]
    products.sort(key=lambda item: (-item.pretax_amount, item.product_name))
    return products, currency


def _sum_costs(products: Sequence[ProductCost]) -> CostTotals:
    return CostTotals(
        pretax_amount=sum(item.pretax_amount for item in products),
        payment_amount=sum(item.payment_amount for item in products),
        outstanding_amount=sum(item.outstanding_amount for item in products),
    )


def _product_cost(
    products: Sequence[ProductCost], terms: Tuple[str, ...]
) -> CostTotals:
    lowered = tuple(term.lower() for term in terms)
    matches = [
        item
        for item in products
        if any(
            term in '{} {}'.format(item.product_code, item.product_name).lower()
            for term in lowered
        )
    ]
    return _sum_costs(matches)


def _parse_billable_usage(payload: Mapping[str, object]) -> List[BillableUsageItem]:
    data = _mapping(payload.get('Data'))
    grouped: MutableMapping[
        Tuple[str, str, str, Optional[float], str], BillableUsageItem
    ] = {}
    for value in _sequence(data.get('Items')):
        item = _mapping(value)
        code = str(item.get('BillingItemCode') or 'unknown')
        name = str(item.get('BillingItem') or item.get('ItemName') or code)
        unit = str(item.get('UsageUnit') or '')
        list_price = _optional_float(item.get('ListPrice'))
        list_price_unit = str(item.get('ListPriceUnit') or '')
        key = (code, name, unit, list_price, list_price_unit)
        current = grouped.get(key)
        if current is None:
            current = BillableUsageItem(
                code=code,
                name=name,
                usage=0,
                unit=unit,
                list_price=list_price,
                list_price_unit=list_price_unit,
                pretax_amount=0,
                payment_amount=0,
            )
            grouped[key] = current
        current.usage += _amount(item, 'Usage')
        current.pretax_amount += _amount(item, 'PretaxAmount')
        current.payment_amount += _amount(item, 'PaymentAmount')
    result = list(grouped.values())
    result.sort(key=lambda item: (-item.pretax_amount, item.name))
    return result


def _parse_daily_cost(payload: Mapping[str, object]) -> CostTotals:
    data = _mapping(payload.get('Data'))
    items = data.get('Items')
    values = (
        _sequence(items.get('Item')) if isinstance(items, Mapping) else _sequence(items)
    )
    return CostTotals(
        pretax_amount=sum(_amount(_mapping(value), 'PretaxAmount') for value in values),
        payment_amount=sum(
            _amount(_mapping(value), 'PaymentAmount') for value in values
        ),
        outstanding_amount=sum(
            _amount(_mapping(value), 'OutstandingAmount') for value in values
        ),
    )


def _xml_int(root: ElementTree.Element, tag: str) -> Optional[int]:
    element = root.find(tag)
    return _optional_int(element.text if element is not None else None)


def _parse_oss_error(body: bytes) -> Tuple[str, str]:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return 'HttpError', 'OSS returned an error response'
    code = root.findtext('Code') or 'HttpError'
    message = root.findtext('Message') or 'OSS returned an error response'
    return code, message


def _parse_cdn_usage(payload: Mapping[str, object]) -> Dict[str, int]:
    usage = _mapping(payload.get('UsageDataPerInterval'))
    result: Dict[str, int] = {}
    for value in _sequence(usage.get('DataModule')):
        item = _mapping(value)
        timestamp = str(item.get('TimeStamp') or '')
        if len(timestamp) < 10:
            continue
        result[timestamp[:10]] = result.get(timestamp[:10], 0) + round(
            _amount(item, 'Value')
        )
    return result
