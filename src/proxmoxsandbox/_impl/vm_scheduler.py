"""Decide which VM to create next given dependency edges and readiness so far.

Pure bookkeeping, no I/O. `InfraCommands.create_sdn_and_vms` drives it: clone
and start stay strictly serial (Proxmox VM ID allocation is not safe to race),
while readiness waits for already-created VMs overlap.
"""

from typing import Dict, Sequence, Set, Tuple

from proxmoxsandbox.schema import DependencyEdge


class VmScheduler:
    """Tracks created/ready VMs by index into `vms_config`.

    Tuple order is a preference, not a guarantee: `next_creatable` returns the
    lowest index whose dependencies are all ready, so a VM blocked on a later
    dependency is skipped until that dependency is up.
    """

    def __init__(self, edges: Sequence[DependencyEdge], count: int) -> None:
        self._count = count
        self._dependencies: Dict[int, Set[int]] = {i: set() for i in range(count)}
        for edge in edges:
            self._dependencies[edge.dependant].add(edge.dependency)
        self._created: Set[int] = set()
        self._ready: Set[int] = set()

    def mark_created(self, index: int) -> None:
        self._created.add(index)

    def mark_ready(self, index: int) -> None:
        self._ready.add(index)

    @property
    def all_created(self) -> bool:
        return len(self._created) == self._count

    def blocking_dependencies(self, index: int) -> Tuple[int, ...]:
        """Dependencies of `index` that are not yet ready, in tuple order."""
        return tuple(sorted(self._dependencies[index] - self._ready))

    def next_creatable(self) -> int | None:
        for index in range(self._count):
            if index not in self._created and not self.blocking_dependencies(index):
                return index
        return None

    def pending_indices(self) -> Tuple[int, ...]:
        """VMs not yet created, in tuple order."""
        return tuple(i for i in range(self._count) if i not in self._created)
