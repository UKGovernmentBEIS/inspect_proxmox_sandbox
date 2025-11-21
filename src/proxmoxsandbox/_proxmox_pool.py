"""Abstract base class for Proxmox instance pool management."""

import asyncio
from abc import ABC, abstractmethod
from collections import defaultdict
from logging import getLogger
from typing import Dict, List, Tuple

from proxmoxsandbox.schema import ProxmoxInstanceConfig, _load_instances_from_env_or_file


class ProxmoxPoolABC(ABC):
    """Abstract base class for managing Proxmox instance pools.

    This ABC defines the interface for different pool management strategies.
    Implementations might use local queues, connect to remote pool servers,
    or implement custom allocation strategies.
    """

    logger = getLogger(__name__)

    @classmethod
    @abstractmethod
    async def initialize(cls) -> None:
        """Initialize the pool (load instances, connect to server, etc).

        This method is called once per task to set up the pool management system.
        For queue-based implementations, this loads instances and creates queues.
        For remote implementations, this might establish a connection to a pool server.
        """
        pass

    @classmethod
    @abstractmethod
    async def acquire_instance(cls, pool_id: str) -> ProxmoxInstanceConfig:
        """Acquire an instance from the pool (may block if unavailable).

        Args:
            pool_id: The pool identifier (e.g., image/AMI name)

        Returns:
            ProxmoxInstanceConfig for the acquired instance

        Raises:
            RuntimeError: If the pool_id doesn't exist
        """
        pass

    @classmethod
    @abstractmethod
    async def release_instance(cls, pool_id: str, instance: ProxmoxInstanceConfig) -> None:
        """Release an instance back to the pool.

        Args:
            pool_id: The pool identifier
            instance: The instance to release back to the pool
        """
        pass

    @classmethod
    def clear_pools(cls) -> None:
        """Clear all pools (optional, primarily for test cleanup).

        Default implementation is a no-op. Override if your implementation
        maintains state that needs to be cleared.
        """
        pass


class QueueBasedProxmoxPool(ProxmoxPoolABC):
    """FIFO queue-based implementation of Proxmox instance pools.

    This is the current/default implementation that uses asyncio.Queue
    for each pool to maintain the 1-1 relationship between Proxmox instances
    and running evaluations.

    Instances are organized into pools by pool_id, where each pool_id typically
    represents a set of Proxmox servers with the same VM images/configuration.
    """

    # Class variables - shared across all uses of this pool implementation
    # Shared queues for instance allocation, keyed by pool_id
    _instance_pools: Dict[str, asyncio.Queue[ProxmoxInstanceConfig]] = {}
    # Locks to prevent race conditions during pool creation
    _pool_locks: Dict[str, asyncio.Lock] = {}

    @classmethod
    async def initialize(cls) -> None:
        """Initialize queue-based pools from infrastructure config.

        Loads instances from PROXMOX_CONFIG_FILE or environment variables,
        groups them by pool_id, and creates a queue for each pool.
        """
        # Load instances from infrastructure config (PROXMOX_CONFIG_FILE or env vars)
        all_instances = _load_instances_from_env_or_file()

        # Group instances by pool_id
        pools: Dict[str, List[ProxmoxInstanceConfig]] = defaultdict(list)
        for instance in all_instances:
            pools[instance.pool_id].append(instance)

        # Create a queue of proxmox instances keyed on the ID of the pool.
        # This ID identifies which evals can run on which Proxmox servers.
        # Examples might be the name of a QCOW image that was used to boot
        # the proxmox server with the needed VMs already inside.
        for pool_id, instances in pools.items():
            # Only create pool once (thread-safe with lock)
            if pool_id not in cls._pool_locks:
                cls._pool_locks[pool_id] = asyncio.Lock()

            async with cls._pool_locks[pool_id]:
                if pool_id not in cls._instance_pools:
                    cls.logger.info(
                        f"Initializing pool '{pool_id}' with {len(instances)} instances"
                    )

                    # Create queue for this pool
                    queue: asyncio.Queue[ProxmoxInstanceConfig] = asyncio.Queue()
                    for instance in instances:
                        queue.put_nowait(instance)

                    cls._instance_pools[pool_id] = queue

    @classmethod
    async def acquire_instance(cls, pool_id: str) -> ProxmoxInstanceConfig:
        """Acquire an instance from the queue (blocks if all instances in use).

        Args:
            pool_id: The pool identifier

        Returns:
            ProxmoxInstanceConfig for the acquired instance

        Raises:
            RuntimeError: If the pool_id doesn't exist
        """
        if pool_id not in cls._instance_pools:
            raise RuntimeError(
                f"Pool '{pool_id}' not found. Available pools: {list(cls._instance_pools.keys())}"
            )

        # This blocks if the queue is empty (all instances in use)
        instance_pool_queue = cls._instance_pools[pool_id]
        return await instance_pool_queue.get()

    @classmethod
    async def release_instance(cls, pool_id: str, instance: ProxmoxInstanceConfig) -> None:
        """Release an instance back to the queue.

        Args:
            pool_id: The pool identifier
            instance: The instance to release back to the pool
        """
        if pool_id in cls._instance_pools:
            cls._instance_pools[pool_id].put_nowait(instance)

    @classmethod
    def clear_pools(cls) -> None:
        """Clear all pools (for test cleanup)."""
        cls._instance_pools.clear()
        cls._pool_locks.clear()
