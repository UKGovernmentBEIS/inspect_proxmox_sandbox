"""Fixtures and tests in a module share one event loop.

The shared sandbox and SDN fixtures are module-scoped and hold loop-bound
state (an `asyncio.Lock` binds to the loop of its first contended acquire), so
a test running on any other loop fails as soon as it contends on one.
"""

import asyncio

import pytest


@pytest.fixture(scope="module")
async def module_fixture_loop() -> asyncio.AbstractEventLoop:
    return asyncio.get_running_loop()


@pytest.fixture(scope="module")
async def module_lock() -> asyncio.Lock:
    return asyncio.Lock()


@pytest.fixture
async def function_fixture_loop() -> asyncio.AbstractEventLoop:
    return asyncio.get_running_loop()


async def _contend(lock: asyncio.Lock) -> None:
    async def hold() -> None:
        async with lock:
            await asyncio.sleep(0)

    await asyncio.gather(hold(), hold())


async def test_fixtures_run_on_the_test_loop(
    module_fixture_loop: asyncio.AbstractEventLoop,
    function_fixture_loop: asyncio.AbstractEventLoop,
) -> None:
    assert asyncio.get_running_loop() is module_fixture_loop
    assert asyncio.get_running_loop() is function_fixture_loop


# Two tests: the first contended acquire binds the lock to its loop, and it is
# the second test that would find it bound to a different one.
@pytest.mark.parametrize("nth_test", [1, 2])
async def test_tests_can_contend_on_a_module_scoped_lock(
    nth_test: int, module_lock: asyncio.Lock
) -> None:
    await _contend(module_lock)
