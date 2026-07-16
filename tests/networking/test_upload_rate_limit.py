import asyncio
from typing import List

import pytest

from blrec.networking.rate_limit import SharedUploadLimiter
from blrec.networking.traffic import TrafficMeter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay
        await asyncio.sleep(0)


async def drain(stream) -> List[bytes]:
    return [chunk async for chunk in stream]


@pytest.mark.asyncio
async def test_concurrent_uploads_share_one_interface_limit() -> None:
    clock = FakeClock()
    meter = TrafficMeter(clock=clock)
    limiter = SharedUploadLimiter(
        lambda interface: 1024,
        meter=meter,
        clock=clock,
        sleep=clock.sleep,
        chunk_bytes=1024,
    )

    first, second = await asyncio.gather(
        drain(limiter.stream('eth0', b'a' * 1024)),
        drain(limiter.stream('eth0', b'b' * 1024)),
    )

    assert first == [b'a' * 1024]
    assert second == [b'b' * 1024]
    assert clock.now == pytest.approx(2.0)
    assert meter.snapshot()[0].upload_total == 2048


@pytest.mark.asyncio
async def test_zero_limit_streams_without_waiting() -> None:
    clock = FakeClock()
    limiter = SharedUploadLimiter(
        lambda interface: 0, clock=clock, sleep=clock.sleep, chunk_bytes=2
    )

    chunks = await drain(limiter.stream('eth0', b'abc'))

    assert chunks == [b'ab', b'c']
    assert clock.now == 0


@pytest.mark.asyncio
async def test_different_interfaces_have_independent_limits() -> None:
    clock = FakeClock()
    limiter = SharedUploadLimiter(
        lambda interface: 1024, clock=clock, sleep=clock.sleep, chunk_bytes=1024
    )

    await asyncio.gather(
        drain(limiter.stream('eth0', b'a' * 1024)),
        drain(limiter.stream('eth1', b'b' * 1024)),
    )

    assert clock.now == pytest.approx(2.0)
