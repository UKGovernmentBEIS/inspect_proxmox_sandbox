"""Discovery cleanup must retire ownership before the final task sweep."""

import asyncio
from functools import partial
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.infra_commands import InfraCommands, ProxmoxTarget
from proxmoxsandbox._impl.sdn_commands import PveIpamStatus
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment


def _tracked_infra(
    monkeypatch,
    *,
    fail_vm=None,
    fail_zone=False,
    registered_id_type: type[int] | type[str] = int,
    inventory_id_type: type[int] | type[str] = int,
    lease_present=True,
):
    api = MagicMock(spec=AsyncProxmoxAPI)
    api.base_url = "https://test"
    infra = InfraCommands.build(api, "pve", "local")
    vms = {101, 102}
    zones = {"abc123z"}
    mapping = PveIpamStatus(
        ip="192.168.20.50",
        mac="02:00:00:00:00:01",
        subnet="192.168.20.0/24",
        vmid=101,
        vnet="abc123v0",
        zone="abc123z",
    )
    for vm_id in vms:
        infra.qemu_commands.register_vm(registered_id_type(vm_id))
    infra.sdn_commands.register_sdn_zone("abc123z")
    infra.sdn_commands.register_ipam_mapping(mapping.to_ipam_mapping())

    async def request(method, path, *, raise_errors=True):
        if path == "/nodes/pve/qemu" and method == "GET":
            return [
                {
                    "vmid": inventory_id_type(vm_id),
                    "name": str(vm_id),
                    "tags": "inspect",
                    "template": 0,
                }
                for vm_id in sorted(vms)
            ]
        if path.startswith("/nodes/pve/qemu/"):
            vm_id = int(path.split("/")[4])
            if method == "POST" and path.endswith("/status/stop"):
                if vm_id == fail_vm:
                    raise RuntimeError("stop failed")
                # Proxmox can accept an asynchronous stop whose worker later
                # fails because the VM has already been deleted.
                return "UPID:pve:stop"
            if method == "DELETE":
                vms.remove(vm_id)
                return "UPID:pve:delete"
            if method == "GET" and path.endswith("/config"):
                return {"net0": "virtio=02:00:00:00:00:01,bridge=abc123v0"}
            if method == "GET" and path.endswith("/status/current"):
                if vm_id in vms:
                    return {"vmid": vm_id, "status": "stopped"}
                if not raise_errors:
                    return {}
                response = httpx.Response(
                    500,
                    text=f"Configuration file 'nodes/pve/qemu-server/{vm_id}.conf' "
                    "does not exist",
                    request=httpx.Request(method, api.base_url + path),
                )
                response.raise_for_status()
        if method == "GET":
            if path == "/cluster/tasks":
                return []
            if path == "/cluster/sdn/zones":
                return [{"zone": zone} for zone in sorted(zones)]
            if path == "/cluster/sdn/vnets":
                return [{"vnet": "abc123v0", "zone": zone} for zone in sorted(zones)]
            if path == "/cluster/sdn/vnets/abc123v0/subnets":
                return []
            if path == "/cluster/sdn/ipams/pve/status":
                return [mapping.model_dump()] if lease_present and 101 in vms else []
        if method == "DELETE":
            if path == "/cluster/sdn/zones/abc123z":
                if fail_zone:
                    raise RuntimeError("zone is still in use")
                zones.remove("abc123z")
                return None
            if path == "/cluster/sdn/vnets/abc123v0" or path.startswith(
                "/cluster/sdn/vnets/abc123v0/ips?"
            ):
                return None
        if method == "PUT" and path == "/cluster/sdn":
            return None
        raise AssertionError((method, path))

    api.request = AsyncMock(side_effect=request)
    monkeypatch.setattr(
        infra.task_wrapper,
        "do_action_and_wait_for_tasks",
        partial(infra.task_wrapper.do_action_and_wait_for_tasks, async_wait_seconds=0),
    )
    InfraCommands.set_instance(ProxmoxTarget("test", 8006, "pve"), infra)
    return infra, api, vms, zones


@pytest.mark.parametrize("registered_id_type", [int, str])
@pytest.mark.parametrize("inventory_id_type", [int, str])
@pytest.mark.parametrize("lease_present", [False, True])
async def test_task_cleanup_after_discovery_does_not_poll_deleted_vms(
    monkeypatch, registered_id_type, inventory_id_type, lease_present
):
    infra, api, vms, zones = _tracked_infra(
        monkeypatch,
        registered_id_type=registered_id_type,
        inventory_id_type=inventory_id_type,
        lease_present=lease_present,
    )

    # sample_init uses discovery cleanup after a readiness failure, before the
    # final task cleanup. Exercise both with real ownership and teardown logic.
    await infra.cleanup_no_id(skip_confirmation=True)
    assert not vms
    assert not zones
    api.request.reset_mock()

    await asyncio.wait_for(
        ProxmoxSandboxEnvironment.task_cleanup("failed-startup", None, cleanup=True),
        timeout=0.1,
    )

    api.request.assert_not_awaited()
    assert not infra.qemu_commands._tracked_vm_ids
    assert not infra.sdn_commands._tracked_sdn_zone_ids
    assert not infra.sdn_commands._tracked_ipam_mappings


@pytest.mark.parametrize("failure", ["vm", "sdn"])
async def test_partial_discovery_cleanup_keeps_only_unfinished_resources(
    monkeypatch, failure
):
    infra, _, vms, zones = _tracked_infra(
        monkeypatch, fail_vm=102 if failure == "vm" else None
    )
    if failure == "sdn":
        monkeypatch.setattr(
            infra.sdn_commands,
            "tear_down_sdn_zones_and_vnets",
            AsyncMock(side_effect=RuntimeError("SDN cleanup failed")),
        )

    with pytest.raises(RuntimeError):
        await infra.cleanup_no_id(skip_confirmation=True)

    assert vms == ({102} if failure == "vm" else set())
    assert infra.qemu_commands._tracked_vm_ids == vms
    assert infra.sdn_commands._tracked_sdn_zone_ids == zones == {"abc123z"}
    assert len(infra.sdn_commands._tracked_ipam_mappings) == 1


async def test_discovery_cleanup_retains_zone_when_teardown_logs_error(monkeypatch):
    infra, _, vms, zones = _tracked_infra(monkeypatch, fail_zone=True)

    await infra.cleanup_no_id(skip_confirmation=True)

    assert not vms
    assert not infra.qemu_commands._tracked_vm_ids
    assert infra.sdn_commands._tracked_sdn_zone_ids == zones == {"abc123z"}
    assert len(infra.sdn_commands._tracked_ipam_mappings) == 1


def test_vm_registry_uses_one_identity_for_integer_and_string_ids(monkeypatch):
    infra, _, _, _ = _tracked_infra(monkeypatch, registered_id_type=str)
    infra.qemu_commands.register_vm("00101")
    assert infra.qemu_commands._tracked_vm_ids == {101, 102}

    infra.qemu_commands.deregister_vms(("101",))
    assert infra.qemu_commands._tracked_vm_ids == {102}


@pytest.mark.parametrize(
    "vm_id", [True, 0, -101, 101.5, None, "101.0", "1e2", " 101", "１２３", ""]
)
def test_vm_registry_rejects_ambiguous_ids_without_changing_ownership(
    monkeypatch, vm_id
):
    infra, _, _, _ = _tracked_infra(monkeypatch)

    with pytest.raises(ValueError, match="VM ID must be"):
        infra.qemu_commands.register_vm(vm_id)
    with pytest.raises(ValueError, match="VM ID must be"):
        infra.qemu_commands.deregister_vms((101, vm_id))

    assert infra.qemu_commands._tracked_vm_ids == {101, 102}


def test_zone_deregistration_preserves_other_zones_and_leases(monkeypatch):
    infra, _, _, _ = _tracked_infra(monkeypatch)
    other_mapping = infra.sdn_commands._tracked_ipam_mappings[0].model_copy(
        update={"zone_id": "def456z", "vnet_id": "def456v0"}
    )
    infra.sdn_commands.register_sdn_zone("def456z")
    infra.sdn_commands.register_ipam_mapping(other_mapping)

    infra.sdn_commands.deregister_sdn_resources("abc123z", ())

    assert infra.sdn_commands._tracked_sdn_zone_ids == {"def456z"}
    assert infra.sdn_commands._tracked_ipam_mappings == [other_mapping]
