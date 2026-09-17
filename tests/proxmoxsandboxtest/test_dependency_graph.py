"""reject_cycles on bare dependency sets, independent of the config model."""

import pytest

from proxmoxsandbox._impl.dependency_graph import reject_cycles

NAMES = ("a", "b", "c", "d")


def test_empty_graph():
    reject_cycles([], ())


def test_no_dependencies():
    reject_cycles([(), (), ()], NAMES)


def test_chain_and_diamond_are_acyclic():
    reject_cycles([(), (0,), (1,)], NAMES)
    reject_cycles([(), (), (0, 1), (2,)], NAMES)


def test_forward_reference_is_acyclic():
    reject_cycles([(1,), ()], NAMES)


def test_self_dependency():
    with pytest.raises(ValueError, match="VM dependency cycle: 'a' -> 'a'"):
        reject_cycles([(0,)], NAMES)


def test_two_cycle():
    with pytest.raises(ValueError, match="VM dependency cycle: 'a' -> 'b' -> 'a'"):
        reject_cycles([(1,), (0,)], NAMES)


def test_three_cycle_reports_nodes_in_path_order():
    # a -> b -> c -> a
    with pytest.raises(ValueError, match="'a' -> 'b' -> 'c' -> 'a'"):
        reject_cycles([(1,), (2,), (0,)], NAMES)


def test_cycle_reported_from_its_own_start_not_the_path_start():
    # a -> b -> c -> b: 'a' is on the path but not in the loop
    with pytest.raises(ValueError, match=r"cycle: 'b' -> 'c' -> 'b'$"):
        reject_cycles([(1,), (2,), (1,)], NAMES)


def test_cycle_in_a_component_not_reachable_from_the_first_vm():
    # a stands alone; c <-> d
    with pytest.raises(ValueError, match="VM dependency cycle"):
        reject_cycles([(), (), (3,), (2,)], NAMES)


def test_shared_dependency_is_not_a_cycle():
    # b and c both depend on a; a is visited twice but never while on the path
    reject_cycles([(), (0,), (0,), (1, 2)], NAMES)
