"""Cycle detection for VM startup dependencies.

`dependencies` maps each VM name to the names it waits for, in vms_config
order. Not part of the public schema.
"""

from typing import Collection, List, Mapping, Set


def reject_cycles(dependencies: Mapping[str, Collection[str]]) -> None:
    """Raise ValueError naming the first cycle found, e.g. 'a' -> 'b' -> 'a'."""
    # DFS in config order. Following an edge to a node already on the path is a
    # loop; a node that has left the path without one is known to be clean.
    visited: Set[str] = set()
    on_path: Set[str] = set()
    path: List[str] = []

    def visit(node: str) -> None:
        visited.add(node)
        on_path.add(node)
        path.append(node)
        for dep in dependencies[node]:
            if dep in on_path:
                cycle = path[path.index(dep) :] + [dep]
                raise ValueError(
                    "VM dependency cycle: " + " -> ".join(repr(n) for n in cycle)
                )
            if dep not in visited:
                visit(dep)
        path.pop()
        on_path.remove(node)

    for node in dependencies:
        if node not in visited:
            visit(node)
