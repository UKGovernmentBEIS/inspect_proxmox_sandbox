"""VM creation retries with a fresh ID when another process took the one we read."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from proxmoxsandbox._impl.qemu_commands import _VM_ID_ATTEMPTS, QemuCommands
from proxmoxsandbox.schema import VmConfig, VmSourceConfig


def _error_500(text: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://pve/api2/json/x")
    return httpx.HTTPStatusError(
        text, request=request, response=httpx.Response(500, text=text, request=request)
    )


def _collision(vm_id: int) -> httpx.HTTPStatusError:
    return _error_500(f"unable to create VM {vm_id}: config file already exists")


def _qemu_with_ids(next_ids: list[str], clone_failures: int) -> QemuCommands:
    """Serve `next_ids` from nextid; the first `clone_failures` clones collide."""
    api = AsyncMock()
    api.wait_for_task = AsyncMock()
    clones = 0

    async def request(method, url, **kwargs):
        nonlocal clones
        if url == "/cluster/nextid":
            return next_ids.pop(0)
        if url.endswith("/clone"):
            clones += 1
            if clones <= clone_failures:
                raise _collision(kwargs["json"]["newid"])
            return "UPID:pve1:clone"
        return "UPID:pve1:other"

    api.request = AsyncMock(side_effect=request)
    qemu = QemuCommands(api, "pve1", "local", MagicMock())
    return qemu


_CONFIG = VmConfig(vm_source_config=VmSourceConfig(existing_vm_template_tag="t"))


@pytest.mark.asyncio
async def test_clone_retries_with_next_id_on_collision():
    qemu = _qemu_with_ids(["4242", "4243"], clone_failures=1)
    with patch.object(qemu, "configure_network_and_tags", AsyncMock()):
        vm_id = await qemu.clone_vm_and_start(
            vm_config=_CONFIG,
            vm_id_to_clone=100,
            sdn_vnet_aliases={},
            preserve_tags=False,
        )
    assert vm_id == 4243
    # Only the clone that succeeded is tracked; 4242 belongs to someone else.
    assert list(qemu._tracked_vms) == [4243]


@pytest.mark.asyncio
async def test_clone_gives_up_after_repeated_collisions():
    ids = [str(5000 + i) for i in range(_VM_ID_ATTEMPTS)]
    qemu = _qemu_with_ids(ids, clone_failures=_VM_ID_ATTEMPTS)
    with pytest.raises(httpx.HTTPStatusError, match="already exists"):
        await qemu.clone_vm_and_start(
            vm_config=_CONFIG,
            vm_id_to_clone=100,
            sdn_vnet_aliases={},
            preserve_tags=False,
        )
    assert qemu._tracked_vms == {}


@pytest.mark.asyncio
async def test_clone_does_not_retry_other_500s():
    api = AsyncMock()
    api.request = AsyncMock(side_effect=["4242", _error_500("storage full")])
    qemu = QemuCommands(api, "pve1", "local", MagicMock())
    with pytest.raises(httpx.HTTPStatusError, match="storage full"):
        await qemu.clone_vm_and_start(
            vm_config=_CONFIG,
            vm_id_to_clone=100,
            sdn_vnet_aliases={},
            preserve_tags=False,
        )
    assert api.request.await_count == 2
