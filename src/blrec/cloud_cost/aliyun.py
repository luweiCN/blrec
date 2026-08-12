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

from .models import (
    BillableUsageItem,
    CdnDailyUsage,
    CdnUsage,
    CloudCostSummary,
    CostTotals,
    CostTrendPoint,
    OssUsage,
    ProductCost,
)

_SIGNATURE_ALGORITHM = 'ACS3-HMAC-SHA256'
_EMPTY_PAYLOAD_HASH = hashlib.sha256(b'').hexdigest()
_BSS_ENDPOINT = 'business.aliyuncs.com'
_BSS_VERSION = '2017-12-14'
_CDN_ENDPOINT = 'cdn.aliyuncs.com'
_CDN_VERSION = '2018-05-10'
_CHINA_TIMEZONE = timezone(timedelta(hours=8))


class HttpSession(Protocol):
    def post(self, url: str, **kwargs: object) -> Any:
        pass


class OpenApiCaller(Protocol):
    async def call(
        self, endpoint: str, action: str, version: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        pass


@dataclass(frozen=True)
class CloudCostConfig:
    access_key_id: Optional[str]
    access_key_secret: Optional[str]
    cdn_domain: str = 'vg.luwei.host'
    oss_bucket: str = 'luwei-vainglory'
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


class AliyunCloudCostService:
    def __init__(
        self,
        config: CloudCostConfig,
        client: OpenApiCaller,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._client = client
        self._now = now
        self._clock = clock
        self._cache_lock = asyncio.Lock()
        self._cached_summary: Optional[CloudCostSummary] = None
        self._cached_at = 0.0

    async def summary(self, *, force_refresh: bool = False) -> CloudCostSummary:
        if not force_refresh and self._cache_is_fresh():
            assert self._cached_summary is not None
            return self._cached_summary
        async with self._cache_lock:
            if not force_refresh and self._cache_is_fresh():
                assert self._cached_summary is not None
                return self._cached_summary
            summary = await self._fetch_summary()
            self._cached_summary = summary
            self._cached_at = self._clock()
            return summary

    def _cache_is_fresh(self) -> bool:
        return self._cached_summary is not None and (
            self._clock() - self._cached_at < self._config.cache_seconds
        )

    async def _fetch_summary(self) -> CloudCostSummary:
        now = self._normalized_now()
        billing_cycle = now.astimezone(_CHINA_TIMEZONE).strftime('%Y-%m')
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

        cycles = _billing_cycles(now.astimezone(_CHINA_TIMEZONE), 6)
        start_time, end_time = _cdn_time_range(now)
        operations: List[Awaitable[Mapping[str, object]]] = [
            self._bill_overview(cycle) for cycle in cycles
        ]
        operations.extend(
            [
                self._oss_billable_usage(billing_cycle),
                self._cdn_usage('traf', start_time=start_time, end_time=end_time),
                self._cdn_usage('acc', start_time=start_time, end_time=end_time),
            ]
        )
        results = await asyncio.gather(*operations, return_exceptions=True)
        overview_results = results[: len(cycles)]
        oss_result, traffic_result, request_result = results[len(cycles) :]
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
        oss = OssUsage(
            bucket=self._config.oss_bucket,
            items=oss_items,
            pretax_amount=oss_cost.pretax_amount,
            payment_amount=oss_cost.payment_amount,
            outstanding_amount=oss_cost.outstanding_amount,
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
            oss=oss,
            cdn=cdn,
            warnings=warnings,
        )

    def _normalized_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    async def _bill_overview(self, billing_cycle: str) -> Mapping[str, object]:
        return await self._client.call(
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
            response = await self._client.call(
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


def _billing_cycles(now: datetime, count: int) -> List[str]:
    year, month = now.year, now.month
    values: List[str] = []
    for offset in range(count - 1, -1, -1):
        month_index = year * 12 + month - 1 - offset
        values.append('{:04d}-{:02d}'.format(month_index // 12, month_index % 12 + 1))
    return values


def _cdn_time_range(now: datetime) -> Tuple[str, str]:
    local_now = now.astimezone(_CHINA_TIMEZONE)
    start = datetime(local_now.year, local_now.month, 1, tzinfo=_CHINA_TIMEZONE)
    return (
        start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    )


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
    grouped: MutableMapping[Tuple[str, str, str], BillableUsageItem] = {}
    for value in _sequence(data.get('Items')):
        item = _mapping(value)
        code = str(item.get('BillingItemCode') or 'unknown')
        name = str(item.get('BillingItem') or item.get('ItemName') or code)
        unit = str(item.get('UsageUnit') or '')
        key = (code, name, unit)
        current = grouped.get(key)
        if current is None:
            current = BillableUsageItem(
                code=code,
                name=name,
                usage=0,
                unit=unit,
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
