"""Cycle detection for VM startup dependencies.

`dependencies[i]` holds the vms_config indices VM i waits for, as built by
`ProxmoxSandboxEnvironmentConfig._dependency_indices()`. Not part of the public
schema.
"""

from typing import Collection, List, Sequence, Set


def reject_cycles(
    dependencies: Sequence[Collection[int]], names: Sequence[str]
) -> None:
    """Raise ValueError naming the first cycle found, e.g. 'a' -> 'b' -> 'a'."""
    # We have a set of unvisited nodes, a set of on-path nodes, and the current
    # path as a stack. Following an edge to a node already on the path is a
    # loop. Visiting a node removes it from unvisited and adds it to on_path;
    # once all its dependencies are visited without finding a cycle it leaves
    # on_path. Nodes in neither set are known not to be part of a loop.
    unvisited = set(range(len(dependencies)))
    on_path: Set[int] = set()
    path: List[int] = []

    def visit(node: int) -> None:
        unvisited.discard(node)
        on_path.add(node)
        path.append(node)
        for dep in sorted(dependencies[node]):
            if dep in on_path:
                cycle = path[path.index(dep) :] + [dep]
                raise ValueError(
                    "VM dependency cycle: " + " -> ".join(repr(names[i]) for i in cycle)
                )
            if dep in unvisited:
                visit(dep)
        path.pop()
        on_path.remove(node)

    while unvisited:
        visit(unvisited.pop())
