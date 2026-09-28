"""Pure scheduling decisions for dependency-ordered VM creation.

No asyncio, no Proxmox: given each VM's dependencies and which VMs have
become ready, which VM should be created next?
"""

import asyncio

import pytest

from proxmoxsandbox._impl.vm_scheduler import VmScheduler


def _drain(scheduler: VmScheduler) -> list[str]:
    """Create everything currently creatable, in the order the scheduler picks."""
    order = []
    while (name := scheduler._next_creatable()) is not None:
        scheduler._mark_created(name)
        order.append(name)
    return order


def test_no_dependencies_creates_in_tuple_order_without_waiting():
    s = VmScheduler({"a": (), "b": (), "c": ()})
    assert _drain(s) == ["a", "b", "c"]


def test_shared_dependency_blocks_until_ready():
    s = VmScheduler({"a": (), "b": ("a",), "c": ("a",)})
    assert _drain(s) == ["a"]
    assert s._next_creatable() is None
    s.mark_ready("a")
    assert _drain(s) == ["b", "c"]


def test_forward_reference_creates_dependency_first():
    s = VmScheduler({"a": ("b",), "b": ()})
    assert _drain(s) == ["b"]
    assert s._next_creatable() is None
    s.mark_ready("b")
    assert _drain(s) == ["a"]


def test_diamond_waits_for_both_dependencies():
    s = VmScheduler({"a": (), "b": (), "c": ("a", "b")})
    assert _drain(s) == ["a", "b"]
    s.mark_ready("a")
    assert s._next_creatable() is None
    s.mark_ready("b")
    assert _drain(s) == ["c"]


def test_tuple_order_is_a_preference_not_a_guarantee():
    """[a, b(depends_on=c), c]: c is created before b even though b precedes it."""
    s = VmScheduler({"a": (), "b": ("c",), "c": ()})
    assert _drain(s) == ["a", "c"]
    s.mark_ready("c")
    assert _drain(s) == ["b"]


def test_pending_indices_reports_blocked_vms():
    s = VmScheduler({"a": (), "b": ("a",), "c": ("a",)})
    _drain(s)
    assert s._pending() == ("b", "c")
    s.mark_ready("a")
    _drain(s)
    assert s._pending() == ()


def test_blocking_dependencies_lists_what_a_vm_is_waiting_on():
    s = VmScheduler({"a": (), "b": (), "c": ("a", "b")})
    _drain(s)
    s.mark_ready("b")
    assert s._blocking_dependencies("c") == ("a",)


async def test_wait_for_progress_wakes_on_mark_ready():
    s = VmScheduler({"a": (), "b": ("a",)})
    _drain(s)
    assert s._next_creatable() is None

    async def later():
        await asyncio.sleep(0)
        s.mark_ready("a")

    asyncio.create_task(later())
    await asyncio.wait_for(s._wait_for_progress(), timeout=1)
    assert s._next_creatable() == "b"


async def test_iteration_raises_first_failure():
    s = VmScheduler({"a": (), "b": ()})
    _drain(s)
    s.mark_failed("a", RuntimeError("VM 100 never came up"))
    s.mark_failed("b", RuntimeError("second, ignored"))
    with pytest.raises(RuntimeError, match="VM 100 never came up"):
        async for _ in s:
            pytest.fail("nothing left to create")


async def test_wait_for_progress_returns_immediately_if_already_signalled():
    s = VmScheduler({"a": ()})
    _drain(s)
    s.mark_ready("a")
    await asyncio.wait_for(s._wait_for_progress(), timeout=1)


def test_all_ready():
    s = VmScheduler({"a": (), "b": ()})
    _drain(s)
    assert not s._all_ready
    s.mark_ready("a")
    assert not s._all_ready
    s.mark_ready("b")
    assert s._all_ready


def test_in_flight_counts_created_but_unresolved_vms():
    s = VmScheduler({"a": (), "b": (), "c": ()})
    _drain(s)
    assert s._in_flight == 3
    s.mark_ready("a")
    s.mark_failed("b", RuntimeError("boom"))
    assert s._in_flight == 1


async def test_wait_for_progress_raises_if_nothing_can_signal():
    """Nothing in flight and nothing already signalled means a hang; fail instead."""
    s = VmScheduler({"a": (), "b": ("a",)})
    _drain(s)
    s.mark_ready("a")
    await s._wait_for_progress()  # consumes the signal
    _drain(s)
    s.mark_ready("b")
    await s._wait_for_progress()
    with pytest.raises(RuntimeError, match="nothing can make progress"):
        await s._wait_for_progress()


async def _ready_soon(s: VmScheduler, name: str) -> None:
    await asyncio.sleep(0)
    s.mark_ready(name)


async def test_async_iteration_yields_in_dependency_order_and_drains():
    # a depends on c; b and c free  ->  b, c, then a once c is ready
    s = VmScheduler({"a": ("c",), "b": (), "c": ()})
    order = []
    async for name in s:
        order.append(name)
        asyncio.create_task(_ready_soon(s, name))
    assert order == ["b", "c", "a"]
    assert s._all_ready


async def test_async_iteration_raises_readiness_failure():
    s = VmScheduler({"a": (), "b": ("a",)})
    seen = []
    with pytest.raises(RuntimeError, match="VM 100 never came up"):
        async for name in s:
            seen.append(name)
            s.mark_failed(name, RuntimeError("VM 100 never came up"))
    assert seen == ["a"]


async def test_failure_during_iteration_body_raises_before_next_yield():
    """No VM is yielded after a readiness task has reported a failure."""
    s = VmScheduler({"a": (), "b": (), "c": (), "d": ()})
    seen = []
    with pytest.raises(RuntimeError, match="VM 100 never came up"):
        async for name in s:
            seen.append(name)
            s.mark_failed(name, RuntimeError("VM 100 never came up"))
    assert seen == ["a"]


async def test_async_iteration_marks_created_on_yield():
    s = VmScheduler({"a": (), "b": ()})
    it = s.__aiter__()
    assert await it.__anext__() == "a"
    assert s._pending() == ("b",)
