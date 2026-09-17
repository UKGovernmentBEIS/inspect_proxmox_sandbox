"""reject_cycles on bare name->dependencies maps, independent of the config model.

test_schema covers the wiring (a cyclic config is a ValidationError); this
covers the algorithm.
"""

import pytest

from proxmoxsandbox._impl.dependency_graph import reject_cycles


@pytest.mark.parametrize(
    "dependencies",
    [
        {},
        {"a": (), "b": (), "c": ()},
        {"a": (), "b": ("a",), "c": ("b",)},  # chain
        {"a": ("b",), "b": ()},  # forward reference
        {"a": (), "b": (), "c": ("a", "b"), "d": ("c",)},  # diamond
        {"a": (), "b": ("a",), "c": ("a",), "d": ("b", "c")},  # a visited twice
    ],
    ids=["empty", "none", "chain", "forward", "diamond", "shared"],
)
def test_acyclic(dependencies):
    reject_cycles(dependencies)


@pytest.mark.parametrize(
    ("dependencies", "cycle"),
    [
        ({"a": ("a",)}, "'a' -> 'a'"),
        ({"a": ("b",), "b": ("a",)}, "'a' -> 'b' -> 'a'"),
        ({"a": ("b",), "b": ("c",), "c": ("a",)}, "'a' -> 'b' -> 'c' -> 'a'"),
        # a leads into the loop but is not part of it
        ({"a": ("b",), "b": ("c",), "c": ("b",)}, "'b' -> 'c' -> 'b'"),
        # the loop is in a component not reachable from the first VM
        ({"a": (), "b": (), "c": ("d",), "d": ("c",)}, "'c' -> 'd' -> 'c'"),
    ],
    ids=["self", "two", "three", "entered-from-outside", "unreachable-from-first"],
)
def test_cyclic(dependencies, cycle):
    with pytest.raises(ValueError, match=f"^VM dependency cycle: {cycle}$"):
        reject_cycles(dependencies)
