"""Pure scheduling decisions for dependency-ordered VM creation.

No asyncio, no Proxmox: given the config's dependency edges and which VMs have
become ready, which VM should be created next?
"""

from proxmoxsandbox._impl.vm_scheduler import VmScheduler
from proxmoxsandbox.schema import DependencyEdge


def _drain(scheduler: VmScheduler) -> list[int]:
    """Create everything currently creatable, in the order the scheduler picks."""
    order = []
    while (idx := scheduler.next_creatable()) is not None:
        scheduler.mark_created(idx)
        order.append(idx)
    return order


def test_no_edges_creates_in_tuple_order_without_waiting():
    s = VmScheduler(edges=(), count=3)
    assert _drain(s) == [0, 1, 2]
    assert s.all_created


def test_await_before_next_vm_blocks_until_ready():
    edges = (
        DependencyEdge(1, 0, "await_before_next_vm"),
        DependencyEdge(2, 0, "await_before_next_vm"),
    )
    s = VmScheduler(edges=edges, count=3)
    assert _drain(s) == [0]
    assert s.next_creatable() is None
    s.mark_ready(0)
    assert _drain(s) == [1, 2]


def test_forward_reference_creates_dependency_first():
    s = VmScheduler(edges=(DependencyEdge(0, 1, "depends_on"),), count=2)
    assert _drain(s) == [1]
    assert s.next_creatable() is None
    s.mark_ready(1)
    assert _drain(s) == [0]


def test_diamond_waits_for_both_dependencies():
    # 2 depends on 0 and 1
    edges = (DependencyEdge(2, 0, "depends_on"), DependencyEdge(2, 1, "depends_on"))
    s = VmScheduler(edges=edges, count=3)
    assert _drain(s) == [0, 1]
    s.mark_ready(0)
    assert s.next_creatable() is None
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
    assert s.pending_indices() == (1, 2)
    s.mark_ready(0)
    _drain(s)
    assert s.pending_indices() == ()


def test_blocking_dependencies_lists_what_a_vm_is_waiting_on():
    edges = (DependencyEdge(2, 0, "depends_on"), DependencyEdge(2, 1, "depends_on"))
    s = VmScheduler(edges=edges, count=3)
    _drain(s)
    s.mark_ready(1)
    assert s.blocking_dependencies(2) == (0,)


def test_all_created_false_while_pending():
    s = VmScheduler(edges=(DependencyEdge(1, 0, "depends_on"),), count=2)
    _drain(s)
    assert not s.all_created
    s.mark_ready(0)
    _drain(s)
    assert s.all_created
