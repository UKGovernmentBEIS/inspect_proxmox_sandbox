"""Building several built-in templates at once."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from proxmoxsandbox._impl.built_in_vm import BuiltInVM


def _built_in_vm(known: dict[str, int]) -> BuiltInVM:
    built_in_vm = BuiltInVM(
        MagicMock(), "pve1", "local", MagicMock(), MagicMock(), MagicMock()
    )
    built_in_vm.known_builtins = AsyncMock(return_value=known)  # type: ignore[method-assign]
    built_in_vm.ensure_static_sdn_exists = AsyncMock()  # type: ignore[method-assign]
    return built_in_vm


async def test_only_missing_built_ins_are_built_after_the_static_sdn_exists():
    built_in_vm = _built_in_vm({"ubuntu24.04": 100})
    order: list[str] = []
    built_in_vm.ensure_static_sdn_exists.side_effect = lambda: order.append("sdn")

    async def ensure_exists(name: str) -> None:
        order.append(name)

    built_in_vm.ensure_exists = ensure_exists  # type: ignore[method-assign]

    await built_in_vm.ensure_exists_many({"ubuntu24.04", "debian13", "kali2025.4"})

    assert order == ["sdn", "debian13", "kali2025.4"]


async def test_nothing_is_created_when_every_built_in_exists():
    built_in_vm = _built_in_vm({"ubuntu24.04": 100})
    built_in_vm.ensure_exists = AsyncMock()  # type: ignore[method-assign]

    await built_in_vm.ensure_exists_many(["ubuntu24.04"])

    built_in_vm.ensure_static_sdn_exists.assert_not_called()
    built_in_vm.ensure_exists.assert_not_called()


async def test_a_failed_build_cancels_the_others():
    built_in_vm = _built_in_vm({})
    cancelled: list[str] = []

    async def ensure_exists(name: str) -> None:
        if name == "debian13":
            raise RuntimeError("build failed")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(name)
            raise

    built_in_vm.ensure_exists = ensure_exists  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="build failed"):
        await asyncio.wait_for(
            built_in_vm.ensure_exists_many(["debian13", "ubuntu24.04"]), timeout=2
        )

    assert cancelled == ["ubuntu24.04"]
