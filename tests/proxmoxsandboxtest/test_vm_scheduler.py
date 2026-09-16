"""Pure scheduling decisions for dependency-ordered VM creation.

No asyncio, no Proxmox: given the config's dependency edges and which VMs have
become ready, which VM should be created next?
"""

import asyncio

import pytest

from proxmoxsandbox._impl.vm_scheduler import VmScheduler
from proxmoxsandbox.schema import DependencyEdge


def _drain(scheduler: VmScheduler) -> list[int]:
    """Create everything currently creatable, in the order the scheduler picks."""
    order = []
    while (idx := scheduler._next_creatable()) is not None:
        scheduler._mark_created(idx)
        order.append(idx)
    return order


def test_no_edges_creates_in_tuple_order_without_waiting():
    s = VmScheduler(edges=(), count=3)
    assert _drain(s) == [0, 1, 2]


def test_await_before_next_vm_blocks_until_ready():
    edges = (
        DependencyEdge(1, 0, "await_before_next_vm"),
        DependencyEdge(2, 0, "await_before_next_vm"),
    )
    s = VmScheduler(edges=edges, count=3)
    assert _drain(s) == [0]
    assert s._next_creatable() is None
    s.mark_ready(0)
    assert _drain(s) == [1, 2]


def test_forward_reference_creates_dependency_first():
    s = VmScheduler(edges=(DependencyEdge(0, 1, "depends_on"),), count=2)
    assert _drain(s) == [1]
    assert s._next_creatable() is None
    s.mark_ready(1)
    assert _drain(s) == [0]


def test_diamond_waits_for_both_dependencies():
    # 2 depends on 0 and 1
    edges = (DependencyEdge(2, 0, "depends_on"), DependencyEdge(2, 1, "depends_on"))
    s = VmScheduler(edges=edges, count=3)
    assert _drain(s) == [0, 1]
    s.mark_ready(0)
    assert s._next_creatable() is None
    s.mark_ready(1)
    assert _drain(s) == [2]


def test_tuple_order_is_a_preference_not_a_guarantee():
    """[a, b(depends_on=c), c]: c is created before b even though b precedes it."""
    s = VmScheduler(edges=(DependencyEdge(1, 2, "depends_on"),), count=3)
    assert _drain(s) == [0, 2]
    s.mark_ready(2)
    assert _drain(s) == [1]


def test_pending_indices_reports_blocked_vms():
    edges = (DependencyEdge(1, 0, "depends_on"), DependencyEdge(2, 0, "depends_on"))
    s = VmScheduler(edges=edges, count=3)
    _drain(s)
    assert s._pending_indices() == (1, 2)
    s.mark_ready(0)
    _drain(s)
    assert s._pending_indices() == ()


def test_blocking_dependencies_lists_what_a_vm_is_waiting_on():
    edges = (DependencyEdge(2, 0, "depends_on"), DependencyEdge(2, 1, "depends_on"))
    s = VmScheduler(edges=edges, count=3)
    _drain(s)
    s.mark_ready(1)
    assert s._blocking_dependencies(2) == (0,)


async def test_wait_for_progress_wakes_on_mark_ready():
    s = VmScheduler(edges=(DependencyEdge(1, 0, "depends_on"),), count=2)
    _drain(s)
    assert s._next_creatable() is None

    async def later():
        await asyncio.sleep(0)
        s.mark_ready(0)

    asyncio.create_task(later())
    await asyncio.wait_for(s._wait_for_progress(), timeout=1)
    assert s._next_creatable() == 1


async def test_iteration_raises_first_failure():
    s = VmScheduler(edges=(), count=2)
    _drain(s)
    s.mark_failed(0, RuntimeError("VM 100 never came up"))
    s.mark_failed(1, RuntimeError("second, ignored"))
    with pytest.raises(RuntimeError, match="VM 100 never came up"):
        async for _ in s:
            pytest.fail("nothing left to create")


async def test_wait_for_progress_returns_immediately_if_already_signalled():
    s = VmScheduler(edges=(), count=1)
    _drain(s)
    s.mark_ready(0)
    await asyncio.wait_for(s._wait_for_progress(), timeout=1)


def test_all_ready():
    s = VmScheduler(edges=(), count=2)
    _drain(s)
    assert not s._all_ready
    s.mark_ready(0)
    assert not s._all_ready
    s.mark_ready(1)
    assert s._all_ready


def test_in_flight_counts_created_but_unresolved_vms():
    s = VmScheduler(edges=(), count=3)
    _drain(s)
    assert s._in_flight == 3
    s.mark_ready(0)
    s.mark_failed(1, RuntimeError("boom"))
    assert s._in_flight == 1


async def test_wait_for_progress_raises_if_nothing_can_signal():
    """Nothing in flight and nothing already signalled means a hang; fail instead."""
    s = VmScheduler(edges=(DependencyEdge(1, 0, "depends_on"),), count=2)
    _drain(s)
    s.mark_ready(0)
    await s._wait_for_progress()  # consumes the signal
    _drain(s)
    s.mark_ready(1)
    await s._wait_for_progress()
    with pytest.raises(RuntimeError, match="nothing can make progress"):
        await s._wait_for_progress()


async def _ready_soon(s: VmScheduler, index: int) -> None:
    await asyncio.sleep(0)
    s.mark_ready(index)


async def test_async_iteration_yields_in_dependency_order_and_drains():
    # 0 depends on 2; 1 free; 2 free  ->  1, 2, then 0 once 2 is ready
    s = VmScheduler(edges=(DependencyEdge(0, 2, "depends_on"),), count=3)
    order = []
    async for index in s:
        order.append(index)
        asyncio.create_task(_ready_soon(s, index))
    assert order == [1, 2, 0]
    assert s._all_ready


async def test_async_iteration_raises_readiness_failure():
    s = VmScheduler(edges=(DependencyEdge(1, 0, "depends_on"),), count=2)
    seen = []
    with pytest.raises(RuntimeError, match="VM 100 never came up"):
        async for index in s:
            seen.append(index)
            s.mark_failed(index, RuntimeError("VM 100 never came up"))
    assert seen == [0]


async def test_failure_during_iteration_body_raises_before_next_yield():
    """No VM is yielded after a readiness task has reported a failure."""
    s = VmScheduler(edges=(), count=4)
    seen = []
    with pytest.raises(RuntimeError, match="VM 100 never came up"):
        async for index in s:
            seen.append(index)
            s.mark_failed(index, RuntimeError("VM 100 never came up"))
    assert seen == [0]


async def test_async_iteration_marks_created_on_yield():
    s = VmScheduler(edges=(), count=2)
    it = s.__aiter__()
    assert await it.__anext__() == 0
    assert s._pending_indices() == (1,)
