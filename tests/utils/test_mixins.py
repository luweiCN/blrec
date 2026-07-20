import pytest

from blrec.utils.mixins import AsyncStoppableMixin


class FailingLifecycle(AsyncStoppableMixin):
    def __init__(self) -> None:
        super().__init__()
        self.start_calls = 0
        self.stop_calls = 0

    async def _do_start(self) -> None:
        self.start_calls += 1

    async def _do_stop(self) -> None:
        self.stop_calls += 1
        if self.stop_calls == 1:
            raise RuntimeError('stop failed')


@pytest.mark.asyncio
async def test_async_stoppable_rolls_back_state_after_stop_failure() -> None:
    lifecycle = FailingLifecycle()
    await lifecycle.start()

    with pytest.raises(RuntimeError, match='stop failed'):
        await lifecycle.stop()
    assert lifecycle.stopped is False

    await lifecycle.stop()
    assert lifecycle.stopped is True
    assert lifecycle.stop_calls == 2
