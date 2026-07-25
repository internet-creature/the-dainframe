"""ConcurrencyLimiter tests: one injected limiter is one shared ceiling
across providers (DESIGN.md §4.8 - resolving a new provider object per model
must not accidentally create a new 'global' semaphore per object)."""

import asyncio

from dainframe.providers.limits import ConcurrencyLimiter


def test_limiter_caps_concurrent_holders():
    limiter = ConcurrencyLimiter(max_concurrent=2)
    peak = 0
    active = 0

    async def _hold():
        nonlocal peak, active
        async with limiter:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1

    async def _main():
        await asyncio.gather(*[_hold() for _ in range(6)])

    asyncio.run(_main())
    assert peak <= 2


def test_one_limiter_is_one_shared_ceiling():
    """two 'providers' (here: two workers) sharing one limiter contend for the
    same slots; separate limiters would each get their own."""
    shared = ConcurrencyLimiter(max_concurrent=1)
    order = []

    async def _worker(name):
        async with shared:
            order.append(f"{name}:in")
            await asyncio.sleep(0)
            order.append(f"{name}:out")

    async def _main():
        await asyncio.gather(_worker("a"), _worker("b"))

    asyncio.run(_main())
    # with a shared ceiling of 1, the sections never interleave
    assert order in (
        ["a:in", "a:out", "b:in", "b:out"],
        ["b:in", "b:out", "a:in", "a:out"],
    )
