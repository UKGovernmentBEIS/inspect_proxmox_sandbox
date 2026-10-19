"""VMs are tracked for task_cleanup from the moment they exist, by int ID."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from proxmoxsandbox._impl.qemu_commands import QemuCommands
from proxmoxsandbox.schema import VmConfig, VmSourceConfig


def _qemu(api) -> QemuCommands:
    return QemuCommands(api, "pve1", "local", MagicMock())


@pytest.mark.asyncio
async def test_clone_is_tracked_even_if_it_never_becomes_ready():
    api = AsyncMock()
    # PVE returns /cluster/nextid as a string; tracking must still key by int
    # so that IDs read back from the VM list (ints) match.
    api.request = AsyncMock(return_value="4242")
    qemu = _qemu(api)

    with patch.object(
        qemu,
        "configure_network_and_tags",
        AsyncMock(side_effect=RuntimeError("QGA never answered")),
    ):
        with pytest.raises(RuntimeError):
            await qemu.clone_vm_and_start(
                vm_config=VmConfig(
                    vm_source_config=VmSourceConfig(existing_vm_template_tag="kali")
                ),
                vm_id_to_clone=100,
                sdn_vnet_aliases={},
                preserve_tags=False,
            )

    assert 4242 in qemu._tracked_vms
    qemu.deregister_vms([4242])
    assert qemu._tracked_vms == {}


@pytest.mark.asyncio
async def test_task_cleanup_forgets_a_vm_that_is_already_gone():
    """A tracked VM deleted behind our back is dropped, not retried for 300s.

    Stopping a missing VM still returns a UPID; the status read afterwards is
    the first call to say the VM does not exist.
    """
    request = httpx.Request("GET", "https://pve/api2/json/x")
    missing = httpx.HTTPStatusError(
        "500",
        request=request,
        response=httpx.Response(
            500,
            text="Configuration file 'nodes/pve1/qemu-server/4242.conf' does not exist",
            request=request,
        ),
    )

    async def fake_request(method, path, **kwargs):
        if path.endswith("/status/stop"):
            return "UPID:pve1:qmstop"
        raise missing

    api = MagicMock()
    api.request = AsyncMock(side_effect=fake_request)
    qemu = _qemu(api)
    qemu.register_vm(
        vm_id=4242,
        vm_config=VmConfig(vm_source_config=VmSourceConfig(built_in="ubuntu24.04")),
    )

    await qemu.task_cleanup()

    assert qemu._tracked_vms == {}


@pytest.mark.asyncio
async def test_task_cleanup_keeps_a_vm_whose_storage_is_missing():
    """A "does not exist" about some other object does not drop the VM."""
    request = httpx.Request("DELETE", "https://pve/api2/json/x")
    other_missing = httpx.HTTPStatusError(
        "500",
        request=request,
        response=httpx.Response(
            500, text="storage 'local-lvm' does not exist", request=request
        ),
    )

    async def fake_request(method, path, **kwargs):
        if path.endswith("/status/stop"):
            return "UPID:pve1:qmstop"
        if path.endswith("/status/current"):
            return {"status": "stopped"}
        raise other_missing

    api = MagicMock()
    api.request = AsyncMock(side_effect=fake_request)
    qemu = _qemu(api)
    qemu.register_vm(
        vm_id=4242,
        vm_config=VmConfig(vm_source_config=VmSourceConfig(built_in="ubuntu24.04")),
    )

    await qemu.task_cleanup()

    assert 4242 in qemu._tracked_vms
