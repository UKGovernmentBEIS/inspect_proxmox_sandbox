"""VM startup dependencies as edges between vms_config indices.

Built by `ProxmoxSandboxEnvironmentConfig._dependency_edges()` from depends_on
and await_before_next_vm; consumed by config validation (cycle detection) and by
`VmScheduler`. Not part of the public schema.
"""

from collections import defaultdict
from typing import Dict, List, Literal, NamedTuple, Sequence, Set, TypeAlias

DependencyOrigin: TypeAlias = Literal["depends_on", "await_before_next_vm"]


class DependencyEdge(NamedTuple):
    """One resolved startup edge: `dependant` waits for `dependency`.

    Both are indices into `ProxmoxSandboxEnvironmentConfig.vms_config`.
    """

    dependant: int
    dependency: int
    origin: DependencyOrigin

    def describe(self, names: Sequence[str]) -> str:
        """Human-readable edge, attributing implied edges to their source."""
        dependant, dependency = names[self.dependant], names[self.dependency]
        text = f"{dependant!r} waits for {dependency!r}"
        if self.origin == "await_before_next_vm":
            text += f" (implied by await_before_next_vm on {dependency!r})"
        return text


def reject_cycles(edges: Sequence[DependencyEdge], names: Sequence[str]) -> None:
    """Raise ValueError naming the cycle, with implied edges attributed."""
    # DFS that tracks the edge path, so a cycle through an implied
    # await_before_next_vm edge is reported with its origin attached.
    adjacency: Dict[int, List[DependencyEdge]] = defaultdict(list)
    for edge in edges:
        adjacency[edge.dependant].append(edge)

    # We have a set of unvisited nodes, a set of on-path nodes, and a stack of
    # the edges making up the current path. If following an edge leads to a
    # node already in the on_path set we have found a loop.
    # Otherwise, visiting a node removes it from unvisited and adds it to
    # on_path. We then visit all its dependencies. If we haven't found a cycle
    # we can remove it from on_path. Nodes which are in neither on_path nor
    # unvisited are confirmed not to be part of a loop so can be skipped.
    # After one pass, any nodes still in unvisited are from disconnected or
    # higher parts of the graph, so just need to be iterated through.
    unvisited = {i for edge in edges for i in (edge.dependant, edge.dependency)}
    on_path: Set[int] = set()
    path: List[DependencyEdge] = []

    def visit(node: int) -> None:
        unvisited.discard(node)
        on_path.add(node)
        for edge in adjacency[node]:
            path.append(edge)
            if edge.dependency in on_path:
                start = next(
                    k for k, e in enumerate(path) if e.dependant == edge.dependency
                )
                cycle = " -> ".join(e.describe(names) for e in path[start:])
                raise ValueError(f"VM dependency cycle: {cycle}")
            if edge.dependency in unvisited:
                visit(edge.dependency)
            path.pop()
        on_path.remove(node)

    while unvisited:
        visit(unvisited.pop())
