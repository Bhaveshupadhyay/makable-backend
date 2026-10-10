import pytest

from app.core.limits import ConcurrencyLimit, PerKeyLimit, ServiceBusyError, TooManyRequestsError


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_a_full_concurrency_limit_answers_busy_and_frees_slots_after() -> None:
    limit = ConcurrencyLimit(2)
    async with limit.slot(), limit.slot():
        with pytest.raises(ServiceBusyError) as busy:
            async with limit.slot():
                pass
        assert busy.value.details == {"retryAfter": 5}
        assert busy.value.headers == {"Retry-After": "5"}
    assert limit.active == 0
    async with limit.slot():
        assert limit.active == 1


async def test_a_slot_is_freed_when_the_request_fails() -> None:
    limit = ConcurrencyLimit(1)
    with pytest.raises(RuntimeError):
        async with limit.slot():
            raise RuntimeError
    assert limit.active == 0


async def test_one_request_at_a_time_per_key() -> None:
    limit = PerKeyLimit(burst=5, every=10)
    async with limit.hold("ada"):
        with pytest.raises(TooManyRequestsError):
            async with limit.hold("ada"):
                pass
        # Other users aren't affected.
        async with limit.hold("grace"):
            pass


async def test_bursts_are_allowed_then_one_per_interval() -> None:
    clock = FakeClock()
    limit = PerKeyLimit(burst=3, every=15, clock=clock)
    for _ in range(3):
        async with limit.hold("ada"):
            pass

    with pytest.raises(TooManyRequestsError) as limited:
        async with limit.hold("ada"):
            pass
    assert limited.value.details == {"retryAfter": 15}

    clock.now += 15
    async with limit.hold("ada"):
        pass
    with pytest.raises(TooManyRequestsError):
        async with limit.hold("ada"):
            pass
