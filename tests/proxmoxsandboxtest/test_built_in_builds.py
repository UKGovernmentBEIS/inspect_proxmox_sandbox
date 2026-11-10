"""Building several built-in templates at once."""

import asyncio
import contextlib
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from proxmoxsandbox._impl.built_in_vm import BuiltInVM

VNET_ID = "bi123v0"


def _built_in_vm(known: dict[str, int], order: list[str]) -> BuiltInVM:
    """A BuiltInVM whose build zone only records its lifetime in `order`."""
    built_in_vm = BuiltInVM(
        MagicMock(), "pve1", "local", MagicMock(), MagicMock(), MagicMock()
    )
    built_in_vm.known_builtins = AsyncMock(return_value=known)  # type: ignore[method-assign]

    @contextlib.asynccontextmanager
    async def build_sdn() -> AsyncIterator[str]:
        order.append("zone up")
        try:
            yield VNET_ID
        finally:
            order.append("zone down")

    built_in_vm.build_sdn = build_sdn  # type: ignore[method-assign]
    return built_in_vm


async def test_missing_built_ins_share_one_build_zone():
    order: list[str] = []
    built_in_vm = _built_in_vm({"ubuntu24.04": 100}, order)

    async def build(name: str, vnet_id: str) -> None:
        order.append(f"{name} on {vnet_id}")

    built_in_vm._build = build  # type: ignore[method-assign]

    await built_in_vm.ensure_exists_many({"ubuntu24.04", "debian13", "kali2025.4"})

    assert order == [
        "zone up",
        f"debian13 on {VNET_ID}",
        f"kali2025.4 on {VNET_ID}",
        "zone down",
    ]


async def test_nothing_is_created_when_every_built_in_exists():
    order: list[str] = []
    built_in_vm = _built_in_vm({"ubuntu24.04": 100}, order)
    built_in_vm._build = AsyncMock()  # type: ignore[method-assign]

    await built_in_vm.ensure_exists_many(["ubuntu24.04"])
    await built_in_vm.ensure_exists("ubuntu24.04")

    assert order == []
    built_in_vm._build.assert_not_called()


async def test_a_failed_build_cancels_the_others_before_the_zone_goes():
    order: list[str] = []
    built_in_vm = _built_in_vm({}, order)

    async def build(name: str, vnet_id: str) -> None:
        if name == "debian13":
            raise RuntimeError("build failed")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            order.append(f"{name} cancelled")
            raise

    built_in_vm._build = build  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="build failed"):
        await asyncio.wait_for(
            built_in_vm.ensure_exists_many(["debian13", "ubuntu24.04"]), timeout=2
        )

    assert order == ["zone up", "ubuntu24.04 cancelled", "zone down"]


async def test_cancelled_builds_finish_their_own_cleanup_before_the_zone_goes():
    """Each build is cancelled once, so what it awaits while unwinding completes."""
    order: list[str] = []
    built_in_vm = _built_in_vm({}, order)
    building = asyncio.Event()

    async def build(name: str, vnet_id: str) -> None:
        try:
            building.set()
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Stands in for destroying the build VM: several awaits long.
            for _ in range(5):
                await asyncio.sleep(0)
            order.append(f"{name} cleaned up")
            raise

    built_in_vm._build = build  # type: ignore[method-assign]

    builds = asyncio.ensure_future(
        built_in_vm.ensure_exists_many(["debian13", "ubuntu24.04"])
    )
    await building.wait()
    builds.cancel()
    with pytest.raises(asyncio.CancelledError):
        await builds

    assert order == [
        "zone up",
        "debian13 cleaned up",
        "ubuntu24.04 cleaned up",
        "zone down",
    ]
