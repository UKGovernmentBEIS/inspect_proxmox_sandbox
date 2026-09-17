"""reject_cycles on bare dependency sets, independent of the config model.

test_schema covers the wiring (a cyclic config is a ValidationError); this
covers the algorithm.
"""

import pytest

from proxmoxsandbox._impl.dependency_graph import reject_cycles

NAMES = ("a", "b", "c", "d")


@pytest.mark.parametrize(
    "dependencies",
    [
        [],
        [(), (), ()],
        [(), (0,), (1,)],  # chain
        [(1,), ()],  # forward reference
        [(), (), (0, 1), (2,)],  # diamond
        [(), (0,), (0,), (1, 2)],  # a visited twice, never while on the path
    ],
    ids=["empty", "none", "chain", "forward", "diamond", "shared"],
)
def test_acyclic(dependencies):
    reject_cycles(dependencies, NAMES)


@pytest.mark.parametrize(
    ("dependencies", "cycle"),
    [
        ([(0,)], "'a' -> 'a'"),
        ([(1,), (0,)], "'a' -> 'b' -> 'a'"),
        ([(1,), (2,), (0,)], "'a' -> 'b' -> 'c' -> 'a'"),
        # a leads into the loop but is not part of it
        ([(1,), (2,), (1,)], "'b' -> 'c' -> 'b'"),
        # a stands alone; the loop is in a component not reachable from it
        ([(), (), (3,), (2,)], "'c' -> 'd' -> 'c'"),
    ],
    ids=["self", "two", "three", "entered-from-outside", "unreachable-from-first"],
)
def test_cyclic(dependencies, cycle):
    with pytest.raises(ValueError, match=f"^VM dependency cycle: {cycle}$"):
        reject_cycles(dependencies, NAMES)
