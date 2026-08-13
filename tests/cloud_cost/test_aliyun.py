import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Mapping, Tuple

import pytest

from blrec.cloud_cost.aliyun import (
    AliyunCloudCostService,
    AliyunOssStatClient,
    CloudCostConfig,
    build_acs3_authorization,
    build_oss4_authorization,
)


class FakeXmlResponse:
    status = 200

    async def __aenter__(self) -> 'FakeXmlResponse':
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def read(self) -> bytes:
        return (
            b'<BucketStat><Storage>12345</Storage><ObjectCount>67</ObjectCount>'
            b'<LastModifiedTime>1723392000</LastModifiedTime></BucketStat>'
        )


class FakeHttpSession:
    def __init__(self) -> None:
        self.url = ''
        self.headers: Mapping[str, str] = {}

    def get(self, url: str, **kwargs: object) -> FakeXmlResponse:
        self.url = url
        self.headers = kwargs['headers']  # type: ignore[assignment]
        return FakeXmlResponse()


def test_acs3_signature_matches_aliyun_reference_vector() -> None:
    headers = {
        'host': 'ecs.cn-shanghai.aliyuncs.com',
        'x-acs-action': 'RunInstances',
        'x-acs-content-sha256': (
            'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
        ),
        'x-acs-date': '2023-10-26T10:22:32Z',
        'x-acs-signature-nonce': '3156853299f313e23d1673dc12e1703d',
        'x-acs-version': '2014-05-26',
    }

    authorization = build_acs3_authorization(
        method='POST',
        canonical_uri='/',
        query={
            'ImageId': 'win2019_1809_x64_dtc_zh-cn_40G_alibase_20230811.vhd',
            'RegionId': 'cn-shanghai',
        },
        headers=headers,
        access_key_id='YourAccessKeyId',
        access_key_secret='YourAccessKeySecret',
    )

    assert authorization == (
        'ACS3-HMAC-SHA256 Credential=YourAccessKeyId,'
        'SignedHeaders=host;x-acs-action;x-acs-content-sha256;x-acs-date;'
        'x-acs-signature-nonce;x-acs-version,'
        'Signature=06563a9e1b43f5dfe96b81484da74bceab24a1d853912eee15083a6f0f3283c0'
    )


def test_oss4_signature_is_stable_for_bucket_stat_request() -> None:
    authorization = build_oss4_authorization(
        method='GET',
        bucket='examplebucket',
        region='cn-hangzhou',
        query={'stat': None},
        headers={
            'host': 'examplebucket.oss-cn-hangzhou.aliyuncs.com',
            'x-oss-content-sha256': 'UNSIGNED-PAYLOAD',
            'x-oss-date': '20250417T011729Z',
        },
        access_key_id='test-access-key-id',
        access_key_secret='test-access-key-secret',
    )

    assert authorization == (
        'OSS4-HMAC-SHA256 Credential=test-access-key-id/'
        '20250417/cn-hangzhou/oss/aliyun_v4_request,'
        'Signature=470aef9aa1a8b5d3ecea4438355fa519c8b8478f0548cb209910e9c10c38c106'
    )


def test_oss4_signature_matches_published_canonical_request() -> None:
    authorization = build_oss4_authorization(
        method='PUT',
        bucket='examplebucket',
        region='cn-hangzhou',
        query={},
        headers={
            'content-disposition': 'attachment',
            'content-length': '3',
            'content-md5': 'ICy5YqxZB1uWSwcVLSNLcA==',
            'content-type': 'text/plain',
            'x-oss-content-sha256': 'UNSIGNED-PAYLOAD',
            'x-oss-date': '20250411T064124Z',
        },
        access_key_id='LTAI****************',
        access_key_secret='yourAccessKeySecret',
        canonical_key='exampleobject',
    )

    assert authorization == (
        'OSS4-HMAC-SHA256 Credential=LTAI****************/'
        '20250411/cn-hangzhou/oss/aliyun_v4_request,'
        'AdditionalHeaders=content-disposition;content-length,'
        'Signature=d3694c2dfc5371ee6acd35e88c4871ac95a7ba01d3a2f476768fe61218590097'
    )


@pytest.mark.asyncio
async def test_oss_stat_client_signs_and_parses_bucket_statistics() -> None:
    session = FakeHttpSession()
    client = AliyunOssStatClient(
        'test-id',
        'test-secret',
        lambda: session,
        now=lambda: datetime(2026, 8, 12, 3, tzinfo=timezone.utc),
    )

    result = await client.get_bucket_stat(
        bucket='luwei-vainglory',
        endpoint='oss-cn-beijing.aliyuncs.com',
        region='cn-beijing',
    )

    assert session.url == 'https://luwei-vainglory.oss-cn-beijing.aliyuncs.com/?stat'
    assert session.headers['Authorization'].startswith('OSS4-HMAC-SHA256 ')
    assert result == {
        'Storage': 12345,
        'ObjectCount': 67,
        'LastModifiedTime': 1_723_392_000,
    }


class FakeOpenApiClient:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, str]]] = []

    async def call(
        self, endpoint: str, action: str, version: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        values = {key: str(value) for key, value in parameters.items()}
        self.calls.append((action, values))
        if action == 'QueryBillOverview':
            cycle = values['BillingCycle']
            if cycle == '2026-08':
                return bill_overview(
                    cycle,
                    [
                        bill_item('oss', '对象存储 OSS', 1.0, 0.8),
                        bill_item('oss', '对象存储 OSS', 0.2, 0.2),
                        bill_item('cdn', '内容分发网络 CDN', 0.3, 0.3),
                        bill_item('ecs', '云服务器 ECS', 5.0, 5.0),
                    ],
                )
            return bill_overview(cycle, [bill_item('oss', '对象存储 OSS', 1, 1)])
        if action == 'DescribeInstanceBill':
            return {
                'Success': True,
                'Data': {
                    'NextToken': '',
                    'Items': [
                        usage_item('storage', '标准存储', '10', 'GB', 0.4),
                        usage_item('storage', '标准存储', '20', 'GB', 0.8),
                        usage_item('requests', '请求次数', '1250', '次', 0),
                    ],
                },
            }
        if action == 'QueryAccountBill':
            return {
                'Success': True,
                'Data': {
                    'Items': {
                        'Item': [
                            {
                                'PretaxAmount': '0.1',
                                'PaymentAmount': '0.08',
                                'OutstandingAmount': '0.02',
                            }
                        ]
                    }
                },
            }
        if action == 'DescribeDomainUsageData':
            values_by_field = {
                'traf': [
                    ('2026-08-10T00:00:00Z', '1000'),
                    ('2026-08-11T00:00:00Z', '500'),
                ],
                'acc': [('2026-08-10T00:00:00Z', '10'), ('2026-08-11T00:00:00Z', '5')],
            }
            return {
                'UsageDataPerInterval': {
                    'DataModule': [
                        {'TimeStamp': timestamp, 'Value': value}
                        for timestamp, value in values_by_field[values['Field']]
                    ]
                }
            }
        raise AssertionError(action)


def bill_overview(
    cycle: str, items: List[Mapping[str, object]]
) -> Mapping[str, object]:
    return {'Success': True, 'Data': {'BillingCycle': cycle, 'Items': {'Item': items}}}


def bill_item(
    code: str, name: str, pretax: float, payment: float
) -> Mapping[str, object]:
    return {
        'ProductCode': code,
        'ProductName': name,
        'Currency': 'CNY',
        'PretaxAmount': pretax,
        'PaymentAmount': payment,
        'OutstandingAmount': pretax - payment,
    }


def usage_item(
    code: str, name: str, usage: str, unit: str, pretax: float
) -> Mapping[str, object]:
    return {
        'BillingItemCode': code,
        'BillingItem': name,
        'Usage': usage,
        'UsageUnit': unit,
        'ListPrice': '0.12',
        'ListPriceUnit': '元/GB/月',
        'PretaxAmount': pretax,
        'PaymentAmount': pretax,
    }


class FakeOssStatClient:
    async def get_bucket_stat(
        self, *, bucket: str, endpoint: str, region: str
    ) -> Mapping[str, object]:
        assert bucket == 'luwei-vainglory'
        assert endpoint == 'oss-cn-beijing.aliyuncs.com'
        assert region == 'cn-beijing'
        return {
            'Storage': 20 * 1024 * 1024 * 1024,
            'ObjectCount': 321,
            'LastModifiedTime': 1_723_392_000,
        }


@pytest.mark.asyncio
async def test_summary_aggregates_costs_usage_and_caches_result() -> None:
    client = FakeOpenApiClient()
    service = AliyunCloudCostService(
        CloudCostConfig(
            access_key_id='test-id',
            access_key_secret='test-secret',
            cdn_domain='vg.luwei.host',
            oss_bucket='luwei-vainglory',
            cache_seconds=600,
        ),
        client,
        FakeOssStatClient(),
        now=lambda: datetime(2026, 8, 12, 3, tzinfo=timezone.utc),
        clock=lambda: 100.0,
        sleeper=lambda _delay: asyncio.sleep(0),
    )

    summary = await service.summary()

    assert summary.status == 'ready'
    assert summary.billing_cycle == '2026-08'
    assert summary.currency == 'CNY'
    assert summary.totals.pretax_amount == pytest.approx(6.5)
    assert summary.totals.payment_amount == pytest.approx(6.3)
    assert summary.totals.outstanding_amount == pytest.approx(0.2)
    assert len(summary.products) == 3
    assert len(summary.trend) == 6
    assert summary.oss is not None
    assert summary.oss.bucket == 'luwei-vainglory'
    assert summary.oss.pretax_amount == pytest.approx(1.2)
    assert summary.oss.storage_bytes == 20 * 1024 * 1024 * 1024
    assert summary.oss.object_count == 321
    assert summary.oss.items[0].usage == pytest.approx(30)
    assert summary.oss.items[0].list_price == pytest.approx(0.12)
    assert len(summary.daily) == 12
    assert summary.daily[0].date == '2026-08-01'
    assert summary.daily[0].pretax_amount == pytest.approx(0.1)
    assert summary.cdn is not None
    assert summary.cdn.domain == 'vg.luwei.host'
    assert summary.cdn.traffic_bytes == 1500
    assert summary.cdn.requests == 15
    assert summary.cdn.pretax_amount == pytest.approx(0.3)
    assert [item.date for item in summary.cdn.daily] == ['2026-08-10', '2026-08-11']
    cdn_calls = [
        parameters
        for action, parameters in client.calls
        if action == 'DescribeDomainUsageData'
    ]
    assert next(item for item in cdn_calls if item['Field'] == 'traf')['Area'] == 'all'
    assert 'Area' not in next(item for item in cdn_calls if item['Field'] == 'acc')
    assert len(client.calls) == 21

    assert await service.summary() is summary
    assert len(client.calls) == 21

    refreshed = await service.summary(force_refresh=True)
    assert refreshed is not summary
    assert len(client.calls) == 42


@pytest.mark.asyncio
async def test_summary_supports_a_historical_billing_cycle() -> None:
    client = FakeOpenApiClient()
    delays: List[float] = []
    current_time = [100.0]

    async def record_sleep(delay: float) -> None:
        delays.append(delay)
        current_time[0] += delay

    service = AliyunCloudCostService(
        CloudCostConfig(
            access_key_id='test-id',
            access_key_secret='test-secret',
            cdn_domain='vg.luwei.host',
            oss_bucket='luwei-vainglory',
        ),
        client,
        FakeOssStatClient(),
        now=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
        clock=lambda: current_time[0],
        sleeper=record_sleep,
    )

    summary = await service.summary(billing_cycle='2026-07')

    assert summary.billing_cycle == '2026-07'
    assert len(summary.daily) == 31
    overview_cycles = [
        values['BillingCycle']
        for action, values in client.calls
        if action == 'QueryBillOverview'
    ]
    assert overview_cycles == [
        '2026-02',
        '2026-03',
        '2026-04',
        '2026-05',
        '2026-06',
        '2026-07',
    ]
    daily_calls = [
        values for action, values in client.calls if action == 'QueryAccountBill'
    ]
    assert daily_calls[0]['BillingDate'] == '2026-07-01'
    assert daily_calls[-1]['BillingDate'] == '2026-07-31'
    assert len(delays) == 37
    assert all(delay == pytest.approx(0.12) for delay in delays)
    cdn_calls = [
        values for action, values in client.calls if action == 'DescribeDomainUsageData'
    ]
    assert cdn_calls[0]['StartTime'] == '2026-06-30T16:00:00Z'
    assert cdn_calls[0]['EndTime'] == '2026-07-31T16:00:00Z'


@pytest.mark.asyncio
async def test_summary_rejects_a_future_billing_cycle() -> None:
    service = AliyunCloudCostService(
        CloudCostConfig(access_key_id='test-id', access_key_secret='test-secret'),
        FakeOpenApiClient(),
        FakeOssStatClient(),
        now=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
        sleeper=lambda _delay: asyncio.sleep(0),
    )

    with pytest.raises(ValueError, match='latest 18 months'):
        await service.summary(billing_cycle='2026-09')


@pytest.mark.asyncio
async def test_unconfigured_summary_never_calls_aliyun() -> None:
    client = FakeOpenApiClient()
    service = AliyunCloudCostService(
        CloudCostConfig(
            access_key_id=None,
            access_key_secret=None,
            cdn_domain='vg.luwei.host',
            oss_bucket='luwei-vainglory',
        ),
        client,
        now=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
        sleeper=lambda _delay: asyncio.sleep(0),
    )

    summary = await service.summary()

    assert summary.status == 'not_configured'
    assert summary.configured is False
    assert 'BLREC_CLOUD_COST_ALIYUN_ACCESS_KEY_ID' in summary.warnings[0]
    assert client.calls == []


class PartialOpenApiClient(FakeOpenApiClient):
    async def call(
        self, endpoint: str, action: str, version: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        if action == 'DescribeInstanceBill':
            raise RuntimeError('forbidden')
        return await super().call(endpoint, action, version, parameters)


@pytest.mark.asyncio
async def test_one_failed_source_returns_partial_summary() -> None:
    service = AliyunCloudCostService(
        CloudCostConfig(
            access_key_id='test-id',
            access_key_secret='test-secret',
            cdn_domain='vg.luwei.host',
            oss_bucket='luwei-vainglory',
        ),
        PartialOpenApiClient(),
        FakeOssStatClient(),
        now=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
        sleeper=lambda _delay: asyncio.sleep(0),
    )

    summary = await service.summary()

    assert summary.status == 'partial'
    assert summary.oss is not None
    assert summary.oss.items == []
    assert any('OSS 用量' in warning for warning in summary.warnings)
