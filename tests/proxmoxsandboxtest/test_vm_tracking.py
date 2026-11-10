"""VMs are tracked for task_cleanup from the moment they exist, by int ID."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from proxmoxsandbox._impl.async_proxmox import ProxmoxTaskError
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
    api.wait_for_task = AsyncMock()
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
    api.wait_for_task = AsyncMock()
    qemu = _qemu(api)
    qemu.register_vm(
        vm_id=4242,
        vm_config=VmConfig(vm_source_config=VmSourceConfig(built_in="ubuntu24.04")),
    )

    await qemu.task_cleanup()

    assert 4242 in qemu._tracked_vms


def _api_with_task_outcomes(outcomes: dict[str, str]) -> MagicMock:
    """Stop and delete return UPIDs; `outcomes` maps a UPID to a failed status."""

    async def fake_request(method, path, **kwargs):
        if path.endswith("/status/stop"):
            return "UPID:pve1:qmstop"
        if path.endswith("/status/current"):
            return {"status": "stopped"}
        return "UPID:pve1:qmdestroy"

    async def wait_for_task(*, upid: str):
        if upid in outcomes:
            raise ProxmoxTaskError(upid, outcomes[upid])
        return {"exitstatus": "OK"}

    api = MagicMock()
    api.request = AsyncMock(side_effect=fake_request)
    api.wait_for_task = AsyncMock(side_effect=wait_for_task)
    return api


@pytest.mark.asyncio
async def test_destroy_vm_succeeds_when_another_delete_won():
    """Two deletes of one VM both get a UPID; the second finds the config gone."""
    api = _api_with_task_outcomes(
        {
            "UPID:pve1:qmdestroy": "Configuration file "
            "'nodes/pve1/qemu-server/4242.conf' does not exist"
        }
    )
    await _qemu(api).destroy_vm(vm_id=4242, name="vm")


@pytest.mark.asyncio
async def test_destroy_vm_raises_when_the_delete_task_fails():
    api = _api_with_task_outcomes(
        {"UPID:pve1:qmdestroy": "storage 'local-lvm' does not exist"}
    )
    with pytest.raises(ProxmoxTaskError, match="local-lvm"):
        await _qemu(api).destroy_vm(vm_id=4242, name="vm")


@pytest.mark.asyncio
async def test_destroy_vm_deletes_after_a_failed_stop_task():
    api = _api_with_task_outcomes(
        {"UPID:pve1:qmstop": "can't lock file '/var/lock/qemu-server/lock-4242.conf'"}
    )
    await _qemu(api).destroy_vm(vm_id=4242, name="vm")
    assert api.request.await_args_list[-1].args == ("DELETE", "/nodes/pve1/qemu/4242")


@pytest.mark.asyncio
async def test_destroy_vm_retries_when_proxmox_cannot_start_a_worker(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    request = httpx.Request("DELETE", "https://pve/api2/json/x")
    no_worker = httpx.HTTPStatusError(
        "500",
        request=request,
        response=httpx.Response(
            500, text="got no worker upid - start worker failed", request=request
        ),
    )
    deletes = 0

    async def fake_request(method, path, **kwargs):
        nonlocal deletes
        if path.endswith("/status/stop"):
            return "UPID:pve1:qmstop"
        if path.endswith("/status/current"):
            return {"status": "stopped"}
        deletes += 1
        if deletes == 1:
            raise no_worker
        return "UPID:pve1:qmdestroy"

    api = MagicMock()
    api.request = AsyncMock(side_effect=fake_request)
    api.wait_for_task = AsyncMock(return_value={"exitstatus": "OK"})

    await _qemu(api).destroy_vm(vm_id=4242, name="vm")

    assert deletes == 2
